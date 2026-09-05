#!/usr/bin/env python3
"""Agent Tracker: one page showing which Claude Code sessions are working on which
product, what each one is doing, what needs you, what it costs, which model each one
runs on, and which Routines are scheduled.

Data comes from two Claude Code Remote MCP tools: list_sessions and list_triggers.
By default the newest results of those tools are lifted straight out of the current
Claude Code transcript (~/.claude/projects/*/*.jsonl), so nothing has to be pasted.
Pass --sessions / --triggers to render from saved JSON instead.

    python3 tracker/tracker.py                      # -> data/dashboard.html
    python3 tracker/tracker.py --out /tmp/x.html
"""
# ponytail: stdlib only. json + re + html do everything this needs.
import argparse
import glob
import html
import json
import os
import re
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = ("list_sessions", "list_triggers")
SESSION_URL = "https://claude.ai/code/{id}"


# ---------------------------------------------------------------- input

def parse_embedded_json(text):
    """First JSON value in `text`. Tool results may be wrapped in prose or tags."""
    dec = json.JSONDecoder()
    starts = [m.start() for m in re.finditer(r'\{"(?:ccr|data)"', text)] or \
             [m.start() for m in re.finditer(r"[\[{]", text)]
    for i in starts:
        try:
            return dec.raw_decode(text, i)[0]
        except ValueError:
            continue
    raise ValueError("no JSON value found in tool result")


def unwrap(obj):
    """{"ccr": {"data": [...]}} | {"data": [...]} | [...]  ->  [...]"""
    if isinstance(obj, dict):
        obj = obj.get("ccr", obj)
    if isinstance(obj, dict):
        obj = obj.get("data", [])
    if not isinstance(obj, list):
        raise ValueError("expected a list of records")
    return obj


def results_from_transcripts(tools):
    """Newest non-error tool_result text per tool, from the most recently written
    Claude Code transcript that has them. Returns ({tool: text}, transcript path)."""
    pattern = os.path.expanduser("~/.claude/projects/*/*.jsonl")
    best = ({}, None)
    for path in sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True):
        uses, found = {}, {}
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                blocks = (rec.get("message") or {}).get("content") if isinstance(rec, dict) else None
                for b in blocks if isinstance(blocks, list) else []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        tool = next((t for t in tools if str(b.get("name", "")).endswith("__" + t)), None)
                        if tool:
                            uses[b.get("id")] = tool
                    elif b.get("type") == "tool_result" and b.get("tool_use_id") in uses and not b.get("is_error"):
                        c = b.get("content")
                        text = c if isinstance(c, str) else "".join(
                            p.get("text", "") for p in (c or []) if isinstance(p, dict))
                        found[uses[b["tool_use_id"]]] = text  # a later call wins
        if all(t in found for t in tools):
            return found, path
        if found and not best[0]:
            best = (found, path)
    return best


def load_records(args):
    texts = {}
    for tool, path in (("list_sessions", args.sessions), ("list_triggers", args.triggers)):
        if path:
            with open(path, encoding="utf-8") as fh:
                texts[tool] = fh.read()
    source = None
    missing = [t for t in TOOLS if t not in texts]
    if missing:
        found, source = results_from_transcripts(missing)
        texts.update(found)
    if "list_sessions" not in texts:
        sys.exit("No list_sessions result found. In this Claude Code session call the "
                 "Claude Code Remote tool list_sessions (limit 100) and list_triggers first, "
                 "or pass --sessions FILE [--triggers FILE].")
    if "list_triggers" not in texts:
        print("warning: no list_triggers result found; Routines section will be empty", file=sys.stderr)
    sessions = unwrap(parse_embedded_json(texts["list_sessions"]))
    triggers = unwrap(parse_embedded_json(texts["list_triggers"])) if "list_triggers" in texts else []
    return sessions, triggers, source


# ---------------------------------------------------------------- normalise

ORIGIN = {"android": "Android", "ios": "iOS", "web": "Web", "desktop": "Desktop",
          "claude_code_cli": "CLI", "claude_code_mcp_seed": "Spawned by a session"}
STATE = {"WORKING": ("Working", "ok"), "AGENTS_RUNNING": ("Agents running", "ok"),
         "REVIEW_READY": ("Review ready", "warn"), "BLOCKED": ("Needs input", "warn"), "FAILED": ("Failed", "bad"),
         "COMPLETED": ("Done", "idle"), "ARCHIVED": ("Archived", "idle"), "IDLE": ("Idle", "idle")}
LIVE = ("WORKING", "AGENTS_RUNNING")
# The session's own last summary is the only place background agents show up; the status
# field is set before they finish. "assembly agent executing EDL" means work is still going.
AGENTS_BUSY = re.compile(r"\b(?:agents?|sub-?agents?|tasks?|workers?|jobs?)\b[^.;]{0,60}?\b(?:executing|running|working|in progress|still going|underway)"
                         r"|\b(?:executing|running|working)\b[^.;]{0,30}?\bin (?:the )?background", re.I)
DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def repo_from_url(url):
    m = re.search(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url or "")
    return f"{m.group(1)}/{m.group(2)}" if m else (url or "")


def enum_tail(value, prefix):
    v = str(value or "")
    return v[len(prefix):] if v.startswith(prefix) else v


def parse_iso(iso):
    if not iso:
        return None
    iso = re.sub(r"(\.\d{1,6})\d*", r"\1", str(iso)).replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return d.astimezone(timezone.utc) if d.tzinfo else d.replace(tzinfo=timezone.utc)


def num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _slug(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def pick_product(repos, outcome, tags, title):
    """Which repo a session belongs to. With several repos attached, a tag or the session
    title naming one of them beats the API's first/output repo (a session called
    "Content creator" with E-Ledger attached first is about content-creator)."""
    if len(repos) > 1:
        by_slug = {_slug(r.split("/")[-1]): r for r in repos}
        for tag in tags:
            if _slug(tag) in by_slug:
                return by_slug[_slug(tag)]
        t = _slug(title)
        for slug, repo in by_slug.items():
            if slug and (slug == t or (len(slug) >= 5 and slug in t)):
                return repo
    return outcome or (repos[0] if repos else "")


def norm_session(s):
    ctx = s.get("session_context") or {}
    ext = s.get("external_metadata") or {}
    pts = s.get("post_turn_summary") or ext.get("post_turn_summary") or {}
    repos = [repo_from_url((x.get("git_repository") or {}).get("url")) for x in ctx.get("sources") or []]
    repos = [r for r in repos if r]
    outcome = next((((o.get("git_repository") or {}).get("git_info") or {}).get("repo")
                    for o in ctx.get("outcomes") or [] if o.get("git_repository")), None)
    product = pick_product(repos, outcome, s.get("tags") or [], s.get("title") or "")
    archived = s.get("session_status") == "SESSION_STATUS_ARCHIVED"
    running = s.get("session_status") == "SESSION_STATUS_RUNNING"
    detail = (pts.get("status_detail") or "").strip()
    bucket = enum_tail(s.get("status_bucket"), "SESSION_STATUS_BUCKET_")
    state = bucket or enum_tail(s.get("session_status"), "SESSION_STATUS_")
    if archived:
        state = "ARCHIVED"
    elif running:
        state = "WORKING"
    elif state in ("REVIEW_READY", "COMPLETED", "IDLE") and AGENTS_BUSY.search(detail):
        state = "AGENTS_RUNNING"
    needs = (pts.get("needs_action") or "").strip()
    live = (s.get("task_summary") or ext.get("task_summary") or "").strip()
    usage, cu = ext.get("usage") or {}, ext.get("context_usage") or {}
    rl = ext.get("rate_limit_info") or {}
    wt = ext.get("worktree_state") if isinstance(ext.get("worktree_state"), dict) else {}
    wt = next((v for v in wt.values() if isinstance(v, dict)), None)
    served = ext.get("last_served_model") or ctx.get("model") or ""
    configured = s.get("configured_model") or ctx.get("model") or ""
    created = parse_iso(s.get("created_at"))
    return {
        "id": str(s.get("id") or ""), "title": s.get("title") or s.get("id") or "Untitled session",
        "product": product, "also": [r for r in repos if r.lower() != product.lower()], "repos": repos,
        "state": state, "needs_you": not archived and (bool(needs) or state in ("REVIEW_READY", "BLOCKED", "FAILED")),
        "api_status": enum_tail(s.get("session_status"), "SESSION_STATUS_"), "bucket": bucket,
        "doing": live if state == "WORKING" and live else detail,
        "recent": (pts.get("recent_action") or "").strip(), "needs": needs,
        "branch": ", ".join(v for v in (ext.get("current_branches") or {}).values() if v),
        "model": served, "configured_model": configured,
        "switched": bool(configured and served and configured != served),
        "effort": ctx.get("effort_level") or ext.get("effort_level") or "",
        "perm": (enum_tail(s.get("permission_mode"), "PERMISSION_MODE_") or ext.get("permission_mode") or "").lower(),
        "origin": ORIGIN.get(s.get("origin"), s.get("origin") or ""),
        "bridge": s.get("environment_kind") == "bridge", "connection": s.get("connection_status") or "",
        "cost": num(usage.get("cost_usd")),
        "tokens": {k: num(usage.get(k)) for k in TOKEN_KEYS},
        "ctx_used": num(cu.get("used_tokens")), "ctx_max": num(cu.get("max_tokens")),
        "ctx_pct": round(100 * (num(cu.get("used_tokens")) or 0) / cu["max_tokens"]) if num(cu.get("max_tokens")) else None,
        "rate": {"type": str(rl.get("rateLimitType") or ""), "status": str(rl.get("status") or ""),
                 "overage": bool(rl.get("isUsingOverage")),
                 "resets": datetime.fromtimestamp(rl["resetsAt"], timezone.utc) if num(rl.get("resetsAt")) else None} if rl else None,
        "cc_version": str(ext.get("container_cc_version") or ""),
        "worktree": {"dirty": bool(wt.get("is_dirty")), "unpushed": num(wt.get("unpushed_count")) or 0} if wt else None,
        "artifacts": [a for a in ext.get("artifacts") or []
                      if isinstance(a, dict) and str(a.get("url", "")).startswith("https://")],
        "parent": s.get("parent_session_id") or "", "tags": [str(t) for t in s.get("tags") or []],
        "unread": bool(s.get("unread")),
        "created": created, "updated": parse_iso(s.get("updated_at")) or created,
    }


def summarize(text, limit=160):
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[email]", " ".join(str(text).split()))
    first = re.split(r"(?<=[.!?])\s", text, 1)[0]
    return first if len(first) <= limit else first[:limit].rsplit(" ", 1)[0] + "…"


def norm_trigger(t, known_products):
    req = t.get("session_request") or {}
    cfg = req.get("config") or {}
    repos = [repo_from_url((x.get("git_repository") or {}).get("url")) for x in cfg.get("sources") or []]
    repos = [r for r in repos if r]
    prompt = (t.get("derived_state") or {}).get("prompt") or ""
    for ev in req.get("events") or [] if not prompt else []:
        msg = ((ev.get("payload") or {}).get("internal_anthropic_catchall") or {}).get("message") or {}
        if isinstance(msg.get("content"), str):
            prompt = msg["content"]
            break
    product = repos[0] if repos else next(
        (p for p in known_products if p.split("/")[-1].lower() in prompt.lower()), "")
    last = t.get("last_run") or {}
    fired, finished = parse_iso(last.get("fired_at")), parse_iso(last.get("finished_at"))
    return {
        "id": str(t.get("id") or ""), "name": t.get("name") or t.get("id") or "Unnamed Routine",
        "product": product, "inferred": not repos and bool(product),
        "cron": t.get("cron_expression") or "", "once": parse_iso(t.get("run_once_at")),
        "enabled": bool(t.get("enabled")), "ended": t.get("ended_reason") or "",
        "suspended": t.get("suspension_reason") or "", "next": parse_iso(t.get("next_run_at")),
        "last_status": enum_tail(last.get("status"), "ROUTINE_RUN_STATUS_"),
        "last_at": finished or fired or parse_iso(t.get("last_fired_at")),
        "last_secs": (finished - fired).total_seconds() if fired and finished else None,
        "model": (t.get("derived_state") or {}).get("model") or "",
        "connectors": [str(c.get("name")) for c in t.get("mcp_connections") or [] if isinstance(c, dict) and c.get("name")],
        "notify": [k for k, v in (((t.get("notifications") or {}).get("channel") or {}).items()) if v],
        "created": parse_iso(t.get("created_at")),
        "desc": summarize(prompt) if prompt else "",
    }


def _days(spec):
    if spec == "*":
        return "every day"
    out = []
    for part in spec.split(","):
        if re.fullmatch(r"\d-\d", part):
            a, b = map(int, part.split("-"))
            out.append(f"{DOW[a]}–{DOW[b]}" if a <= 7 and b <= 7 else None)
        elif part.isdigit() and int(part) <= 7:
            out.append(DOW[int(part)])
        else:
            return None
    return None if None in out else ", ".join(out)


def human_cron(expr):
    parts = expr.split()
    if len(parts) != 5:
        return expr
    minute, hour, dom, mon, dow = parts
    days = _days(dow) if dom == "*" and mon == "*" else None
    if days is None or not minute.isdigit():
        return expr + " (UTC)"
    mm = f"{int(minute):02d}"
    if hour.isdigit():
        when_ = f"{int(hour):02d}:{mm} UTC"
    elif re.fullmatch(r"\d+-\d+", hour):
        a, b = hour.split("-")
        when_ = f"hourly {int(a):02d}:{mm}–{int(b):02d}:{mm} UTC"
    elif hour == "*":
        when_ = f"hourly at :{mm}"
    elif re.fullmatch(r"\*/\d+", hour):
        when_ = f"every {hour[2:]} h at :{mm}"
    else:
        return expr + " (UTC)"
    return f"{when_}, {days}"


# ---------------------------------------------------------------- formatting

esc = html.escape


def compact(n):
    """1234567 -> 1.2M, 88263 -> 88.3k. Tokens are read as magnitudes, not counted."""
    if num(n) is None:
        return ""
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + unit
    return f"{int(n)}"


def money(x):
    return f"${x:,.2f}" if num(x) is not None else ""


def duration(seconds):
    if num(seconds) is None:
        return ""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    parts = [f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m and not d else ""]
    return " ".join(p for p in parts if p) or "0m"


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def when(d, prefix=""):
    if not d:
        return ""
    return f'<span>{esc(prefix)}<time datetime="{d.isoformat()}">{d:%b} {d.day}, {d:%H:%M} UTC</time></span>'


def session_href(sid):
    return SESSION_URL.format(id=sid) if re.fullmatch(r"session_[A-Za-z0-9]+", sid) else ""


def pill(label, kind, live=False):
    dot = '<span class="dot" aria-hidden="true"></span>' if live else ""
    return f'<span class="pill {kind}">{dot}{esc(label)}</span>'


def chip(text, cls=""):
    return f'<span class="chip {cls}">{esc(text)}</span>'


def meta_line(items):
    return f'<p class="meta">{"".join(f"<span>{i}</span>" if not i.startswith("<span") else i for i in items if i)}</p>'


# ---------------------------------------------------------------- render: rows

def session_details(s, titles, now):
    """Everything the API knows about one session, as a folded definition list."""
    tk = s["tokens"]
    model = esc(s["model"]) + (f' <small>(configured {esc(s["configured_model"])})</small>' if s["switched"] else "")
    ctx = (f'{compact(s["ctx_used"])} of {compact(s["ctx_max"])} ({s["ctx_pct"]}%)'
           if s["ctx_pct"] is not None else "")
    rate = ""
    if s["rate"]:
        rate = " · ".join(x for x in [s["rate"]["type"].replace("_", "-"),
                                      f'resets {when(s["rate"]["resets"])}' if s["rate"]["resets"] else "",
                                      "using overage" if s["rate"]["overage"] else "", s["rate"]["status"]] if x)
    tree = ""
    if s["worktree"]:
        tree = ", ".join(x for x in ["uncommitted changes" if s["worktree"]["dirty"] else "",
                                     plural(s["worktree"]["unpushed"], "unpushed commit") if s["worktree"]["unpushed"] else ""] if x) or "clean"
    rows = [
        ("Session", f'<code>{esc(s["id"])}</code>'),
        ("Started", when(s["created"])),
        ("Last activity", when(s["updated"])),
        ("Active span", duration((s["updated"] - s["created"]).total_seconds()) if s["created"] and s["updated"] else ""),
        ("API state", " · ".join(x.lower().replace("_", " ") for x in (s["api_status"], s["bucket"]) if x)),
        ("Model", model), ("Effort", esc(s["effort"])), ("Permissions", esc(s["perm"])),
        ("Runs on", "your machine (Remote Control)" if s["bridge"] else "Anthropic cloud"),
        ("Started from", esc(s["origin"])), ("Connection", esc(s["connection"].replace("_", " "))),
        ("Repositories", esc(", ".join(s["repos"]))), ("Tags", esc(", ".join(s["tags"]))),
        ("Continues", esc(titles.get(s["parent"], s["parent"])) if s["parent"] else ""),
        ("Input tokens", compact(tk["input_tokens"])), ("Output tokens", compact(tk["output_tokens"])),
        ("Cache read", compact(tk["cache_read_tokens"])), ("Cache write", compact(tk["cache_write_tokens"])),
        ("Cost so far", money(s["cost"])), ("Context", ctx), ("Usage window", rate),
        ("Claude Code", esc(s["cc_version"])), ("Working tree", tree),
        ("Artifacts", ", ".join(f'<a href="{esc(a["url"])}">{esc(a.get("title") or "artifact")}</a>' for a in s["artifacts"])),
    ]
    dl = "".join(f"<dt>{esc(k)}</dt><dd>{v}</dd>" for k, v in rows if v)
    return f'<details class="more"><summary>All details</summary><dl class="kv">{dl}</dl></details>'


def session_row(s, titles, now, show_product=False):
    label, kind = STATE.get(s["state"], (s["state"].replace("_", " ").capitalize() or "Unknown", "idle"))
    href = session_href(s["id"])
    title = (f'<a class="title" href="{esc(href)}" data-session="{esc(s["id"])}">{esc(s["title"])}</a>' if href
             else f'<span class="title">{esc(s["title"])}</span>')
    chips = "".join([
        chip(s["product"].split("/")[-1]) if show_product and s["product"] else "",
        chip("New", "new") if s["unread"] else "",
        "".join(chip("also " + r.split("/")[-1]) for r in s["also"]),
    ])
    lines = []
    if s["doing"]:
        lines.append(f'<p class="doing">{esc(s["doing"])}</p>')
    if s["needs"]:
        lines.append(f'<p class="next"><b>Needs you:</b> {esc(s["needs"])}</p>')
    elif s["recent"] and s["recent"] != s["doing"]:
        lines.append(f'<p class="recent">{esc(s["recent"])}</p>')
    meta = [
        f"<code>{esc(s['branch'])}</code>" if s["branch"] else "",
        esc(s["model"]) + (f" · effort {esc(s['effort'])}" if s["effort"] else ""),
        esc(s["origin"]), "on your machine" if s["bridge"] else "",
        f"{s['ctx_pct']}% context" if s["ctx_pct"] is not None else "",
        f"{compact(s['tokens']['output_tokens'])} tokens out" if s["tokens"]["output_tokens"] else "",
        money(s["cost"]),
        "turn ended, agents reported still running" if s["state"] == "AGENTS_RUNNING" else "",
        f"continues “{esc(titles[s['parent']])}”" if s["parent"] in titles else "",
        when(s["updated"], "updated "),
    ] + [f'<a href="{esc(a["url"])}">{esc(a.get("title") or "artifact")}</a>' for a in s["artifacts"]]
    return (f'<li class="row">{pill(label, kind, live=s["state"] in LIVE)}'
            f'<div><p class="head">{title}{chips}</p>{"".join(lines)}{meta_line(meta)}'
            f'{session_details(s, titles, now)}</div></li>')


def trigger_row(t, show_product=True):
    if t["last_status"] == "FAILED":
        p = pill("Last run failed", "bad")
    elif t["ended"] or t["suspended"]:
        p = pill("Ended" if t["ended"] else "Suspended", "idle")
    elif not t["enabled"]:
        p = pill("Paused", "idle")
    else:
        p = pill("Scheduled", "ok")
    chips = chip(t["product"].split("/")[-1] + ("?" if t["inferred"] else "")) if show_product and t["product"] else ""
    meta = [
        esc(human_cron(t["cron"])) if t["cron"] else (when(t["once"], "once at ") if t["once"] else ""),
        when(t["next"], "next ") if t["enabled"] and t["next"] else "",
        (when(t["last_at"], f"last run {t['last_status'].lower() or 'recorded'} ") if t["last_at"]
         else ("never run" if not t["last_status"] else "")),
        f"took {duration(t['last_secs'])}" if t["last_secs"] is not None else "",
        esc(t["model"]) if t["model"] else "default model",
        "connectors: " + esc(", ".join(t["connectors"])) if t["connectors"] else "",
        "notifies by " + esc(", ".join(t["notify"])) if t["notify"] else "",
    ]
    desc = f'<p class="doing">{esc(t["desc"])}</p>' if t["desc"] else ""
    return (f'<li class="row">{p}<div><p class="head"><span class="title">{esc(t["name"])}</span>{chips}</p>'
            f'{desc}{meta_line(meta)}</div></li>')


def rows(items, empty):
    return f'<ul class="rows">{"".join(items)}</ul>' if items else f'<p class="empty">{esc(empty)}</p>'


# ---------------------------------------------------------------- render: usage

def aggregate(sessions, key):
    out = {}
    for s in sessions:
        k = key(s) or "unknown"
        a = out.setdefault(k.lower(), {"name": k, "sessions": [], "cost": 0.0, "out": 0, "in": 0, "has_usage": 0})
        a["sessions"].append(s)
        a["cost"] += s["cost"] or 0
        a["out"] += s["tokens"]["output_tokens"] or 0
        a["in"] += sum(s["tokens"][k2] or 0 for k2 in ("input_tokens", "cache_read_tokens", "cache_write_tokens"))
        a["has_usage"] += s["cost"] is not None
    return sorted(out.values(), key=lambda a: (-a["cost"], -len(a["sessions"])))


def bar_table(title, groups, total, used_by=False, label=lambda g: g["name"]):
    lis = []
    for g in groups:
        pct = 100 * g["cost"] / total if total else 0
        names = [x["title"] for x in g["sessions"]]
        by = (f'<p class="recent">used by {esc(", ".join(names[:3]))}'
              f'{esc(f" and {len(names) - 3} more") if len(names) > 3 else ""}</p>') if used_by else ""
        stats = meta_line([plural(len(g["sessions"]), "session"),
                           f"{compact(g['out'])} tokens out" if g["out"] else "",
                           f"{compact(g['in'])} tokens in and cached" if g["in"] else ""])
        lis.append(
            f'<li><div class="bt-head"><span>{esc(label(g))}</span><span class="bt-val">{money(g["cost"])}</span></div>'
            f'<div class="bar" role="img" aria-label="{pct:.0f} percent of spend" title="{pct:.1f}% of spend">'
            f'<i style="width:{pct:.1f}%"></i></div>{stats}{by}</li>')
    return f'<div class="bt"><h3>{esc(title)}</h3><ul class="rows">{"".join(lis)}</ul></div>'


def usage_section(ss, ts):
    with_usage = [s for s in ss if s["cost"] is not None]
    total = sum(s["cost"] for s in with_usage)
    out_tokens = sum(s["tokens"]["output_tokens"] or 0 for s in ss)
    in_tokens = sum(sum(s["tokens"][k] or 0 for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens")) for s in ss)
    by_model = aggregate(ss, lambda s: s["model"])
    by_product = aggregate(ss, lambda s: s["product"].split("/")[-1] if s["product"] else "no repository")
    routine_models = {}
    for t in ts:
        routine_models[t["model"] or "default model"] = routine_models.get(t["model"] or "default model", 0) + 1
    routines_note = "; ".join(f"{n} on {esc(m)}" for m, n in routine_models.items())
    figures = "".join(f'<div class="fig"><b>{v}</b><span>{k}</span></div>' for k, v in [
        ("total spend", money(total) or "$0.00"),
        ("tokens out", compact(out_tokens) or "0"),
        ("tokens in and cached", compact(in_tokens) or "0"),
        ("models in use", str(len([m for m in by_model if m["name"] != "unknown"]))),
    ])
    note = (f'<p class="note">Spend is the sum of what the API reports per session, for '
            f'{len(with_usage)} of {len(ss)} sessions.{" Routines: " + routines_note + "." if routines_note else ""}</p>')
    return (f'<h2>Usage <span class="n">{money(total)}</span></h2><div class="figures">{figures}</div>'
            f'<div class="bts">{bar_table("By model", by_model, total, used_by=True)}'
            f'{bar_table("By product", by_product, total)}</div>{note}')


# ---------------------------------------------------------------- render: page

def render(raw_sessions, raw_triggers, now, hop_url=""):
    ss = [norm_session(s) for s in raw_sessions if isinstance(s, dict)]
    titles = {s["id"]: s["title"] for s in ss}
    products = {}
    for s in ss:
        products.setdefault(s["product"].lower(), [s["product"], []])[1].append(s)
    ts = [norm_trigger(t, [v[0] for k, v in products.items() if k]) for t in raw_triggers if isinstance(t, dict)]
    for t in ts:
        if t["product"]:
            products.setdefault(t["product"].lower(), [t["product"], []])
    epoch = datetime.fromtimestamp(0, timezone.utc)
    order = {"WORKING": 0, "AGENTS_RUNNING": 0}
    needs = sorted((s for s in ss if s["needs_you"]), key=lambda s: s["updated"] or epoch, reverse=True)
    failed_routines = [t for t in ts if t["last_status"] == "FAILED" or t["suspended"]]
    working = sum(s["state"] in LIVE for s in ss)
    active_products = [k for k, v in products.items() if k]

    needs_html = (f'<h2>Needs you <span class="n">{len(needs) + len(failed_routines)}</span></h2>'
                  + rows([session_row(s, titles, now, show_product=True) for s in needs]
                         + [trigger_row(t) for t in failed_routines], "Nothing is waiting on you."))

    def latest(key):
        return max((s["updated"] or epoch for s in products[key][1]), default=epoch)

    product_html = []
    for key in sorted(active_products, key=latest, reverse=True) + ([""] if "" in products else []):
        name, sessions = products[key]
        sessions.sort(key=lambda s: (order.get(s["state"], 1 if s["needs_you"] else 2), -(s["updated"] or epoch).timestamp()))
        open_rows = [session_row(s, titles, now) for s in sessions if s["state"] != "ARCHIVED"]
        archived = [session_row(s, titles, now) for s in sessions if s["state"] == "ARCHIVED"]
        routines = [trigger_row(t, show_product=False) for t in ts if t["product"].lower() == key and key]
        live = sum(s["state"] in LIVE for s in sessions)
        spend = sum(s["cost"] or 0 for s in sessions)
        counts = " · ".join(x for x in [
            f"{live} working" if live else "", f"{len(open_rows)} open" if open_rows else "",
            plural(len(routines), "routine") if routines else "",
            f"{len(archived)} archived" if archived else "", f"{money(spend)} spent" if spend else ""] if x)
        heading = (f'<a href="https://github.com/{esc(name)}">{esc(name.split("/")[-1])}</a>' if key
                   else "No repository")
        html_ = [f'<section class="product"><h2>{heading} <small>{esc(counts)}</small></h2>',
                 rows(open_rows, "No open sessions.")]
        if routines:
            html_.append(f'<h3>Routines</h3>{rows(routines, "")}')
        if archived:
            html_.append(f'<details class="archived"><summary>{plural(len(archived), "archived session")}</summary>'
                         f'{rows(archived, "")}</details>')
        html_.append("</section>")
        product_html.append("".join(html_))

    unassigned = [trigger_row(t) for t in ts if not t["product"]]
    routines_html = (f'<h2>Routines without a repository <span class="n">{len(unassigned)}</span></h2>'
                     f'{rows(unassigned, "")}') if unassigned else ""

    counts = (f'<p class="counts"><span><b>{working}</b>working</span><span><b>{len(needs)}</b>need you</span>'
              f'<span><b>{sum(1 for s in ss if s["state"] != "ARCHIVED")}</b>open sessions</span>'
              f'<span><b>{len(active_products)}</b>products</span>'
              f'<span><b>{sum(t["enabled"] for t in ts)}</b>routines on</span></p>')
    rated = max((s for s in ss if s["rate"] and s["rate"]["resets"] and s["rate"]["resets"] > now),
                key=lambda s: s["updated"] or epoch, default=None)
    window = ""
    if rated:
        r = rated["rate"]
        window = (f'<span>{esc(r["type"].replace("_", "-"))} usage window resets '
                  f'<time datetime="{r["resets"].isoformat()}">{r["resets"]:%b} {r["resets"].day}, {r["resets"]:%H:%M} UTC</time>'
                  f'{" · using overage" if r["overage"] else ""}</span>')

    return PAGE.format(
        css=CSS.replace("@LIGHT@", LIGHT).replace("@DARK@", DARK), js=JS,
        snapshot=f'<time id="snap" datetime="{now.isoformat()}">{now:%b} {now.day}, {now:%H:%M} UTC</time>',
        window=window, counts=counts, needs=needs_html, usage=usage_section(ss, ts),
        products="".join(product_html), routines=routines_html, n_sessions=len(ss),
        hop=json.dumps(hop_url if hop_url.startswith("https://") else ""))


LIGHT = """--bg:#F3F6F4;--surface:#FFFFFF;--ink:#18211D;--muted:#5C6964;--line:#D8E0DB;
--accent:#0B6B67;--ok:#19733F;--ok-soft:#DBF2E3;--warn:#8F4700;--warn-soft:#FBE8CF;
--bad:#AF2318;--bad-soft:#FADFDC;--idle:#5C6964;--idle-soft:#E7ECE9;"""
DARK = """--bg:#0F1512;--surface:#161D19;--ink:#E4EBE7;--muted:#96A39D;--line:#25302B;
--accent:#5BC8BC;--ok:#63CF8B;--ok-soft:#153A26;--warn:#F2B35C;--warn-soft:#3C2A10;
--bad:#F08A80;--bad-soft:#41201C;--idle:#96A39D;--idle-soft:#222B27;"""

CSS = """
:root{@LIGHT@
--sans:"Atkinson Hyperlegible",system-ui,-apple-system,"Segoe UI",sans-serif;
--display:"Bricolage Grotesque",var(--sans);
--mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){@DARK@}}
:root[data-theme="dark"]{@DARK@}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 var(--sans);-webkit-font-smoothing:antialiased}
a{color:var(--accent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.wrap{max-width:900px;margin:0 auto;padding:28px 20px 56px}
h1{font:700 32px/1.05 var(--display);margin:0;letter-spacing:-.015em;text-wrap:balance}
.sub{color:var(--muted);margin:8px 0 0;display:flex;flex-wrap:wrap;gap:6px 14px;font-size:14px;align-items:center}
.counts{margin:22px 0 0;display:flex;flex-wrap:wrap;gap:10px 26px;font-variant-numeric:tabular-nums}
.counts b{font:700 24px/1 var(--display);margin-right:7px;color:var(--ink)}
.counts span{color:var(--muted);font-size:12.5px;text-transform:uppercase;letter-spacing:.07em;display:inline-flex;align-items:baseline}
h2{font:600 13px/1 var(--sans);text-transform:uppercase;letter-spacing:.09em;color:var(--muted);margin:38px 0 10px;display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
h2 .n{color:var(--ink);font:500 13px var(--mono)}
.product h2{font:600 21px/1.2 var(--display);text-transform:none;letter-spacing:-.01em;color:var(--ink)}
.product h2 a{color:inherit;text-decoration:none} .product h2 a:hover{text-decoration:underline}
.product h2 small{font:400 13px/1 var(--sans);color:var(--muted)}
h3{font:600 12px/1 var(--sans);text-transform:uppercase;letter-spacing:.09em;color:var(--muted);margin:14px 0 8px}
ul{list-style:none;margin:0;padding:0}
.rows{background:var(--surface);border:1px solid var(--line);border-radius:6px}
.rows>li{border-top:1px solid var(--line);padding:12px 14px} .rows>li:first-child{border-top:0}
.row{display:grid;grid-template-columns:124px minmax(0,1fr);gap:4px 14px}
.row p,.bt p{margin:0}
.pill{display:inline-flex;align-items:center;gap:6px;height:22px;padding:0 9px;border-radius:999px;font-size:12px;font-weight:700;letter-spacing:.02em;white-space:nowrap;justify-self:start;margin-top:1px}
.ok{color:var(--ok);background:var(--ok-soft)} .warn{color:var(--warn);background:var(--warn-soft)}
.bad{color:var(--bad);background:var(--bad-soft)} .idle{color:var(--idle);background:var(--idle-soft)}
.dot{width:7px;height:7px;border-radius:50%;background:currentColor;animation:pulse 1.6s ease-in-out infinite}
@keyframes pulse{50%{opacity:.3}}
@media (prefers-reduced-motion:reduce){.dot{animation:none}}
.head{display:flex;flex-wrap:wrap;align-items:center;gap:4px 8px}
.title{font-weight:700;color:var(--ink);text-decoration:none;overflow-wrap:anywhere} a.title:hover{text-decoration:underline}
.chip{font:500 11.5px/1 var(--mono);color:var(--muted);border:1px solid var(--line);border-radius:4px;padding:3px 6px;white-space:nowrap}
.chip.new{color:var(--accent);border-color:var(--accent)}
.doing,.recent{margin-top:3px;overflow-wrap:anywhere} .recent{color:var(--muted);font-size:14px}
.next{margin-top:3px;color:var(--warn)}
.meta{margin-top:5px !important;display:flex;flex-wrap:wrap;gap:2px 0;color:var(--muted);font-size:13px;font-variant-numeric:tabular-nums}
.meta>span+span::before{content:"·";margin:0 7px;opacity:.6}
.meta code,.kv code{font:500 12.5px var(--mono);color:var(--ink);background:var(--idle-soft);padding:1px 5px;border-radius:3px;overflow-wrap:anywhere}
.meta time{white-space:nowrap}
details.archived{margin-top:8px} summary{cursor:pointer;color:var(--muted);font-size:13px;padding:6px 2px}
details.more{margin-top:4px} details.more summary{font-size:12.5px;padding:2px 0;display:inline-block}
.kv{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:3px 14px;margin:6px 0 2px;font-size:13px;font-variant-numeric:tabular-nums}
.kv dt{color:var(--muted)} .kv dd{margin:0;overflow-wrap:anywhere} .kv small{color:var(--muted)}
.figures{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:0 0 14px}
.fig{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:12px 14px}
.fig b{display:block;font:700 22px/1.1 var(--display);font-variant-numeric:tabular-nums;color:var(--ink)}
.fig span{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.07em}
.bts{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;align-items:start}
.bt h3{margin-top:0}
.bt-head{display:flex;justify-content:space-between;gap:10px;font-weight:700;overflow-wrap:anywhere}
.bt-val{font-variant-numeric:tabular-nums;white-space:nowrap}
.bar{height:6px;background:var(--idle-soft);border-radius:3px;margin:7px 0 2px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--accent);border-radius:3px}
.note{color:var(--muted);font-size:13px;margin:10px 0 0}
.empty{color:var(--muted);padding:14px;border:1px dashed var(--line);border-radius:6px;margin:0}
.stale{color:var(--warn);background:var(--warn-soft);padding:2px 9px;border-radius:999px;font-size:12px;font-weight:700}
footer{margin-top:48px;color:var(--muted);font-size:13px;border-top:1px solid var(--line);padding-top:14px;line-height:1.6}
footer code{font:12.5px var(--mono)}
@media (max-width:600px){.wrap{padding:22px 14px 48px}.row{grid-template-columns:1fr;gap:6px}.counts{gap:8px 18px}.counts b{font-size:21px}.kv{grid-template-columns:1fr;gap:0 0}.kv dt{margin-top:6px}}
"""

JS = """
(function(){
  var rtf=new Intl.RelativeTimeFormat(undefined,{numeric:'auto'});
  var units=[['year',31536e6],['month',2592e6],['day',864e5],['hour',36e5],['minute',6e4]];
  function rel(d){var diff=d-Date.now();for(var i=0;i<units.length;i++){var u=units[i];
    if(Math.abs(diff)>=u[1]||i===units.length-1)return rtf.format(Math.round(diff/u[1]),u[0]);}}
  document.querySelectorAll('time[datetime]').forEach(function(t){var d=new Date(t.getAttribute('datetime'));
    if(isNaN(d))return;t.title=t.textContent;t.textContent=rel(d);});
  // Phones and tablets: the artifact viewer only lets https links out, so a session title
  // goes to the hop page (hop/open.html#session_id), which hands off to the Claude app.
  var hop=HOP_URL, ua=navigator.userAgent, touchMac=navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1;
  if(hop&&(/Android|iPhone|iPad|iPod/i.test(ua)||touchMac)){
    document.querySelectorAll('a.title[data-session]').forEach(function(a){
      var id=a.getAttribute('data-session'); if(/^session_[A-Za-z0-9]+$/.test(id))a.href=hop+'#'+id;
    });
  }
  var snap=document.getElementById('snap');
  if(snap&&Date.now()-new Date(snap.getAttribute('datetime'))>6*36e5)document.getElementById('stale').hidden=false;
})();
"""

PAGE = """<title>Agent Tracker</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:wght@600;700&family=Atkinson+Hyperlegible:ital,wght@0,400;0,700;1,400&family=JetBrains+Mono:wght@400;500&display=swap">
<style>{css}</style>
<div class="wrap">
<header>
  <h1>Agent Tracker</h1>
  <p class="sub"><span>Snapshot {snapshot}</span><span>{n_sessions} most recent sessions on this account</span>{window}<span id="stale" class="stale" hidden>Snapshot is over 6 hours old</span></p>
  {counts}
</header>
<main>
{needs}
{usage}
{products}
{routines}
</main>
<footer>Refresh from any Claude Code session on the Agent-tracker repository by running the <code>agent-tracker</code> skill, or let the scheduled Routine republish it. Costs and tokens are the figures the session API reports for this account.</footer>
</div>
<script>var HOP_URL={hop};{js}</script>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sessions", help="saved list_sessions result (JSON or raw tool text)")
    ap.add_argument("--triggers", help="saved list_triggers result (JSON or raw tool text)")
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "dashboard.html"))
    args = ap.parse_args()
    sessions, triggers, source = load_records(args)
    hop_path = os.path.join(ROOT, "tracker", "HOP_URL")   # where hop/open.html is hosted, once it is
    hop_url = open(hop_path, encoding="utf-8").read().strip() if os.path.exists(hop_path) else ""
    page = render(sessions, triggers, datetime.now(timezone.utc), hop_url)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(page)
    print(f"{len(sessions)} sessions, {len(triggers)} routines"
          + (f" (from {source})" if source else "") + f" -> {args.out}")


if __name__ == "__main__":
    main()
