"""Content scanner: runs every pattern over text and returns redacted findings."""
from __future__ import annotations

import re
from pathlib import Path

from .findings import Finding
from .patterns import PATTERNS, Pattern
from .policy import Policy
from .util import fingerprint, is_binary, looks_like_placeholder, path_matches, redact, shannon_entropy

IGNORE_PRAGMA = re.compile(r"secure-check:\s*(?:ignore|allow)", re.I)
DOTENV_NAME = re.compile(r"^\.env(?:\..+)?$")
DOTENV_EXAMPLE = re.compile(r"^\.env\.(?:example|sample|template|dist)$", re.I)
DOTENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*['\"]?([^'\"\s#]{12,})['\"]?\s*(?:#.*)?$")
# Bare variable names whose values are not secrets even when random-looking.
DOTENV_BENIGN = re.compile(r"(?i)(?:_MODEL|_LOCATION|_REGION|_BUCKET|_PROJECT|_URL|_URI|_HOST|_PORT|_PATH|_DIR|_NAME|_ID|_VERSION|_MODE|_BACKEND|_LEVEL|_ENV|_PAPER|_CAPITAL|_SHARES|_FORMAT|_TZ|_TIMEZONE|_SHA|_COMMIT|_REF|_BRANCH|_TAG|_FLAG|_FLAGS|_EMAIL|_LOCALE|_LANG|_USER|_USERNAME|_DEBUG|_TIMEOUT|_RETRIES|_COUNT|_LIMIT|_SIZE|_WORKERS|_ENABLED|_DISABLED)$")
# A dotenv value is treated as a secret only when the KEY name reads like a credential; a
# random-shaped value under an innocuous key (COMMIT_SHA, FEATURE_FLAGS) is not, and any value
# with a recognisable secret SHAPE is already caught by the pattern rules above.
DOTENV_SECRET_NAME = re.compile(r"(?i)(?:KEY|SECRET|TOKEN|PASS|PASSWD|PWD|CRED|AUTH|PRIVATE|SIGNING|CERT|SALT|SEED|DSN|WEBHOOK|BEARER|SESSION|APIKEY)")
# Unquoted `name = value` in config-style files (.properties, .ini, .yaml, .toml, .cfg, .conf).
# Code files are excluded on purpose: there the quoted generic rule applies.
CONFIG_EXT = (".properties", ".ini", ".cfg", ".conf", ".yaml", ".yml", ".toml", ".envrc", ".txt",
              ".config", ".xml", ".sh", ".bash", ".zsh", ".ksh", ".fish", ".mk", ".make")
CONFIG_NAMES = ("dockerfile", "containerfile", "makefile", "procfile", ".flaskenv")
CONFIG_SECRET_LINE = re.compile(
    r"^\s*(?:(?:export|ENV|ARG|set|setx|SET)\s+)?([A-Za-z0-9_.\-]*(?:password|passwd|pwd|secret|token|api[_\-]?key|apikey|"
    r"private[_\-]?key|access[_\-]?key|client[_\-]?secret|auth[_\-]?token)[A-Za-z0-9_.\-]*)\s*[:= ]\s*['\"]?([^'\"\s#<]{8,})['\"]?\s*(?:#.*)?$",
    re.I)
CONFIG_BENIGN_VALUE = re.compile(r"(?i)^(?:true|false|none|null|e[n]v|environment|file|vault|keychain|\$\{.*\}|\$[A-Z_]+|/[\w./-]*|https?://[^\s]+|[a-z_]+\(.*\)|os\.environ.*)$")


class Scanner:
    def __init__(self, policy: Policy):
        self.policy = policy
        self.patterns: list[Pattern] = [p for p in PATTERNS if p.id not in policy.disable_rules]
        self.allow_regex = [re.compile(x) for x in policy.allow_patterns]

    # -- allow-listing --------------------------------------------------------
    def _allowed_value(self, value: str) -> bool:
        fp = fingerprint(value)
        if any(fp.startswith(a) for a in self.policy.allow_fingerprints if len(a) >= 8):
            return True
        return any(r.search(value) for r in self.allow_regex)

    def path_allowed(self, path: str) -> bool:
        return path_matches(path, self.policy.allow_paths)

    # -- core -------------------------------------------------------------------
    def scan_text(self, text: str, path: str, *, commit: str | None = None,
                  line_offset: int = 0) -> list[Finding]:
        if self.path_allowed(path):
            return []
        out: list[Finding] = []
        base = path.rsplit("/", 1)[-1]
        dotenv = bool(DOTENV_NAME.match(base)) and not DOTENV_EXAMPLE.match(base)
        for idx, line in enumerate(text.splitlines(), 1):
            if IGNORE_PRAGMA.search(line):
                continue
            lineno = idx + line_offset
            seen_here: set[str] = set()
            for pat in self.patterns:
                for m in pat.regex.finditer(line):
                    value = m.group(pat.group) or m.group(0)
                    if not self._accept(value, pat):
                        continue
                    fp = fingerprint(value)
                    if fp in seen_here:
                        continue
                    seen_here.add(fp)
                    out.append(Finding(
                        rule=pat.id, severity=pat.severity, path=path, line=lineno,
                        message=pat.description, fingerprint=fp, preview=redact(value),
                        commit=commit, fix=pat.fix, tags=list(pat.tags),
                    ))
            if not dotenv and not seen_here and (base.lower().endswith(CONFIG_EXT)
                    or base.lower() in CONFIG_NAMES or base.lower().startswith("dockerfile")):
                m = CONFIG_SECRET_LINE.match(line)
                if m and not CONFIG_BENIGN_VALUE.match(m.group(2)):
                    value = m.group(2)
                    fp = fingerprint(value)
                    if shannon_entropy(value) >= 2.5 and not looks_like_placeholder(value) and not self._allowed_value(value):
                        seen_here.add(fp)
                        out.append(Finding(
                            rule="config_secret_assignment", severity="medium", path=path, line=lineno,
                            message=f"Unquoted secret-named setting '{m.group(1)}' with a real-looking value in a config file",
                            fingerprint=fp, preview=redact(value), commit=commit,
                            fix="Move the value to an environment variable or a gitignored file; rotate it if it was ever pushed.",
                        ))
            if dotenv:
                m = DOTENV_LINE.match(line)
                if m and DOTENV_SECRET_NAME.search(m.group(1)) and not DOTENV_BENIGN.search(m.group(1)):
                    value = m.group(2)
                    fp = fingerprint(value)
                    if fp not in seen_here and shannon_entropy(value) >= 3.0 \
                            and not looks_like_placeholder(value) and not self._allowed_value(value):
                        out.append(Finding(
                            rule="dotenv_value", severity="high", path=path, line=lineno,
                            message=f"Value assigned to {m.group(1)} in a dotenv file",
                            fingerprint=fp, preview=redact(value), commit=commit,
                            fix="This file must be gitignored; rotate the value if it was ever pushed.",
                        ))
        return out

    def _accept(self, value: str, pat: Pattern) -> bool:
        if pat.min_entropy and shannon_entropy(value) < pat.min_entropy:
            return False
        if looks_like_placeholder(value):
            return False
        if self._allowed_value(value):
            return False
        return True

    def scan_bytes(self, data: bytes, path: str, *, commit: str | None = None) -> list[Finding]:
        if is_binary(data):
            return []
        if len(data) > self.policy.max_file_bytes:
            data = data[: self.policy.max_file_bytes]
        return self.scan_text(data.decode("utf-8", "replace"), path, commit=commit)

    def scan_file(self, root: Path, relpath: str) -> list[Finding]:
        p = root / relpath
        try:
            data = p.read_bytes()
        except (OSError, ValueError):
            return []
        return self.scan_bytes(data, relpath)
