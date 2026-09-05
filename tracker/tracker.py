#!/usr/bin/env python3
"""Agent Tracker: one page showing which Claude Code sessions are working on which
product, what each one is doing, what needs you, and which Routines are scheduled.

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
         "REVIEW_READY": ("Review ready", "warn"), "FAILED": ("Failed", "bad"),
         "COMPLETED": ("Done", "idle"), "ARCHIVED": ("Archived", "idle"), "IDLE": ("Idle", "idle")}
LIVE = ("WORKING", "AGENTS_RUNNING")
# The session's own last summary is the only place background agents show up; the status
# field is set before they finish. "assembly agent executing EDL" means work is still going.
AGENTS_BUSY = re.compile(r"\b(?:agents?|sub-?agents?|tasks?|workers?|jobs?)\b[^.;]{0,60}?\b(?:executing|running|working|in progress|still going|underway)"
                         r"|\b(?:executing|running|working)\b[^.;]{0,30}?\bin (?:the )?background", re.I)
DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


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


def norm_session(s):
    ctx = s.get("session_context") or {}
    ext = s.get("external_metadata") or {}
    pts = s.get("post_turn_summary") or ext.get("post_turn_summary") or {}
    repos = [repo_from_url((x.get("git_repository") or {}).get("url")) for x in ctx.get("sources") or []]
    repos = [r for r in repos if r]
    outcome = next((((o.get("git_repository") or {}).get("git_info") or {}).get("repo")
                    for o in ctx.get("outcomes") or [] if o.get("git_repository")), None)
    product = outcome or (repos[0] if repos else "")
    archived = s.get("session_status") == "SESSION_STATUS_ARCHIVED"
    running = s.get("session_status") == "SESSION_STATUS_RUNNING"
    detail = (pts.get("status_detail") or "").strip()
    state = enum_tail(s.get("status_bucket"), "SESSION_STATUS_BUCKET_") or enum_tail(s.get("session_status"), "SESSION_STATUS_")
    if archived:
        state = "ARCHIVED"
    elif running:
        state = "WORKING"
    elif state in ("REVIEW_READY", "COMPLETED", "IDLE") and AGENTS_BUSY.search(detail):
        state = "AGENTS_RUNNING"
    needs = (pts.get("needs_action") or "").strip()
    live = (s.get("task_summary") or ext.get("task_summary") or "").strip()
    usage, cu = ext.get("usage") or {}, ext.get("context_usage") or {}
    return {
        "id": str(s.get("id") or ""), "title": s.get("title") or s.get("id") or "Untitled session",
        "product": product, "also": [r for r in repos if r.lower() != product.lower()],
        "state": state, "needs_you": not archived and (bool(needs) or state in ("REVIEW_READY", "FAILED")),
        "doing": live if state == "WORKING" and live else detail,
        "recent": (pts.get("recent_action") or "").strip(), "needs": needs,
        "branch": ", ".join(v for v in (ext.get("current_branches") or {}).values() if v),
        "model": ext.get("last_served_model") or ctx.get("model") or "",
        "origin": ORIGIN.get(s.get("origin"), s.get("origin") or ""),
        "bridge": s.get("environment_kind") == "bridge",
        "cost": usage.get("cost_usd"),
        "ctx_pct": round(100 * cu.get("used_tokens", 0) / cu["max_tokens"]) if cu.get("max_tokens") else None,
        "artifacts": [a for a in ext.get("artifacts") or []
                      if isinstance(a, dict) and str(a.get("url", "")).startswith("https://")],
        "parent": s.get("parent_session_id") or "", "unread": bool(s.get("unread")),
        "updated": parse_iso(s.get("updated_at")) or parse_iso(s.get("created_at")),
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
    return {
        "id": str(t.get("id") or ""), "name": t.get("name") or t.get("id") or "Unnamed Routine",
        "product": product, "inferred": not repos and bool(product),
        "cron": t.get("cron_expression") or "", "once": parse_iso(t.get("run_once_at")),
        "enabled": bool(t.get("enabled")), "ended": t.get("ended_reason") or "",
        "suspended": t.get("suspension_reason") or "", "next": parse_iso(t.get("next_run_at")),
        "last_status": enum_tail(last.get("status"), "ROUTINE_RUN_STATUS_"),
        "last_at": parse_iso(last.get("finished_at") or last.get("fired_at") or t.get("last_fired_at")),
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
        when = f"{int(hour):02d}:{mm} UTC"
    elif re.fullmatch(r"\d+-\d+", hour):
        a, b = hour.split("-")
        when = f"hourly {int(a):02d}:{mm}–{int(b):02d}:{mm} UTC"
    elif hour == "*":
        when = f"hourly at :{mm}"
    elif re.fullmatch(r"\*/\d+", hour):
        when = f"every {hour[2:]} h at :{mm}"
    else:
        return expr + " (UTC)"
    return f"{when}, {days}"


# ---------------------------------------------------------------- render

esc = html.escape


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


def session_row(s, titles, show_product=False):
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
    meta = []
    if s["branch"]:
        meta.append(f"<span><code>{esc(s['branch'])}</code></span>")
    if s["model"]:
        meta.append(f"<span>{esc(s['model'])}</span>")
    if s["origin"]:
        meta.append(f"<span>{esc(s['origin'])}</span>")
    if s["bridge"]:
        meta.append("<span>on your machine</span>")
    if s["ctx_pct"] is not None:
        meta.append(f"<span>{s['ctx_pct']}% context</span>")
    if isinstance(s["cost"], (int, float)):
        meta.append(f"<span>${s['cost']:,.2f}</span>")
    if s["state"] == "AGENTS_RUNNING":
        meta.append("<span>turn ended, agents reported still running</span>")
    if s["parent"] and s["parent"] in titles:
        meta.append(f"<span>continues “{esc(titles[s['parent']])}”</span>")
    meta.append(when(s["updated"], "updated "))
    for a in s["artifacts"]:
        meta.append(f'<span><a href="{esc(a["url"])}">{esc(a.get("title") or "artifact")}</a></span>')
    return (f'<li class="row">{pill(label, kind, live=s["state"] in LIVE)}'
            f'<div><p class="head">{title}{chips}</p>{"".join(lines)}'
            f'<p class="meta">{"".join(meta)}</p></div></li>')


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
    meta = []
    if t["cron"]:
        meta.append(f"<span>{esc(human_cron(t['cron']))}</span>")
    elif t["once"]:
        meta.append(when(t["once"], "once at "))
    if t["enabled"] and t["next"]:
        meta.append(when(t["next"], "next "))
    if t["last_at"]:
        meta.append(when(t["last_at"], f"last run {t['last_status'].lower() or 'recorded'} "))
    elif not t["last_status"]:
        meta.append("<span>never run</span>")
    desc = f'<p class="doing">{esc(t["desc"])}</p>' if t["desc"] else ""
    return (f'<li class="row">{p}<div><p class="head"><span class="title">{esc(t["name"])}</span>{chips}</p>'
            f'{desc}<p class="meta">{"".join(meta)}</p></div></li>')


def rows(items, empty):
    return f'<ul class="rows">{"".join(items)}</ul>' if items else f'<p class="empty">{esc(empty)}</p>'


def render(raw_sessions, raw_triggers, now):
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

    parts = []
    parts.append(rows([session_row(s, titles, show_product=True) for s in needs] +
                      [trigger_row(t) for t in failed_routines],
                      "Nothing is waiting on you."))
    needs_html = f'<h2>Needs you <span class="n">{len(needs) + len(failed_routines)}</span></h2>{parts[-1]}'

    def latest(key):
        return max((s["updated"] or epoch for s in products[key][1]), default=epoch)

    product_html = []
    for key in sorted(active_products, key=latest, reverse=True) + ([""] if "" in products else []):
        name, sessions = products[key]
        sessions.sort(key=lambda s: (order.get(s["state"], 1 if s["needs_you"] else 2), -(s["updated"] or epoch).timestamp()))
        open_rows = [session_row(s, titles) for s in sessions if s["state"] != "ARCHIVED"]
        archived = [session_row(s, titles) for s in sessions if s["state"] == "ARCHIVED"]
        routines = [trigger_row(t, show_product=False) for t in ts if t["product"].lower() == key and key]
        live = sum(s["state"] in LIVE for s in sessions)
        counts = " · ".join(x for x in [
            f"{live} working" if live else "", f"{len(open_rows)} open" if open_rows else "",
            f"{len(routines)} routine{'s' if len(routines) != 1 else ''}" if routines else "",
            f"{len(archived)} archived" if archived else ""] if x)
        heading = (f'<a href="https://github.com/{esc(name)}">{esc(name.split("/")[-1])}</a>' if key
                   else "No repository")
        html_ = [f'<section class="product"><h2>{heading} <small>{esc(counts)}</small></h2>',
                 rows(open_rows, "No open sessions.")]
        if routines:
            html_.append(f'<h3>Routines</h3>{rows(routines, "")}')
        if archived:
            html_.append(f'<details class="archived"><summary>{len(archived)} archived session{"s" if len(archived) != 1 else ""}</summary>'
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

    return PAGE.format(
        css=CSS.format(light=LIGHT, dark=DARK), js=JS,
        snapshot=f'<time id="snap" datetime="{now.isoformat()}">{now:%b} {now.day}, {now:%H:%M} UTC</time>',
        counts=counts, needs=needs_html, products="".join(product_html), routines=routines_html,
        n_sessions=len(ss))


LIGHT = """--bg:#F3F6F4;--surface:#FFFFFF;--ink:#18211D;--muted:#5C6964;--line:#D8E0DB;
--accent:#0B6B67;--ok:#19733F;--ok-soft:#DBF2E3;--warn:#8F4700;--warn-soft:#FBE8CF;
--bad:#AF2318;--bad-soft:#FADFDC;--idle:#5C6964;--idle-soft:#E7ECE9;"""
DARK = """--bg:#0F1512;--surface:#161D19;--ink:#E4EBE7;--muted:#96A39D;--line:#25302B;
--accent:#5BC8BC;--ok:#63CF8B;--ok-soft:#153A26;--warn:#F2B35C;--warn-soft:#3C2A10;
--bad:#F08A80;--bad-soft:#41201C;--idle:#96A39D;--idle-soft:#222B27;"""

CSS = """
:root{{{light}
--sans:"Atkinson Hyperlegible",system-ui,-apple-system,"Segoe UI",sans-serif;
--display:"Bricolage Grotesque",var(--sans);
--mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{{dark}}}}}
:root[data-theme="dark"]{{{dark}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 var(--sans);-webkit-font-smoothing:antialiased}}
a{{color:var(--accent)}}
:focus-visible{{outline:2px solid var(--accent);outline-offset:2px}}
.wrap{{max-width:900px;margin:0 auto;padding:28px 20px 56px}}
h1{{font:700 32px/1.05 var(--display);margin:0;letter-spacing:-.015em;text-wrap:balance}}
.sub{{color:var(--muted);margin:8px 0 0;display:flex;flex-wrap:wrap;gap:6px 14px;font-size:14px;align-items:center}}
.counts{{margin:22px 0 0;display:flex;flex-wrap:wrap;gap:10px 26px;font-variant-numeric:tabular-nums}}
.counts b{{font:700 24px/1 var(--display);margin-right:7px}}
.counts span{{color:var(--muted);font-size:12.5px;text-transform:uppercase;letter-spacing:.07em;display:inline-flex;align-items:baseline}}
.counts b{{color:var(--ink)}}
h2{{font:600 13px/1 var(--sans);text-transform:uppercase;letter-spacing:.09em;color:var(--muted);margin:38px 0 10px;display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}}
h2 .n{{color:var(--ink);font:500 13px var(--mono)}}
.product h2{{font:600 21px/1.2 var(--display);text-transform:none;letter-spacing:-.01em;color:var(--ink)}}
.product h2 a{{color:inherit;text-decoration:none}} .product h2 a:hover{{text-decoration:underline}}
.product h2 small{{font:400 13px/1 var(--sans);color:var(--muted)}}
h3{{font:600 12px/1 var(--sans);text-transform:uppercase;letter-spacing:.09em;color:var(--muted);margin:14px 0 8px}}
ul{{list-style:none;margin:0;padding:0}}
.rows{{background:var(--surface);border:1px solid var(--line);border-radius:6px}}
.row{{display:grid;grid-template-columns:124px minmax(0,1fr);gap:4px 14px;padding:12px 14px;border-top:1px solid var(--line)}}
.row:first-child{{border-top:0}}
.row p{{margin:0}}
.pill{{display:inline-flex;align-items:center;gap:6px;height:22px;padding:0 9px;border-radius:999px;font-size:12px;font-weight:700;letter-spacing:.02em;white-space:nowrap;justify-self:start;margin-top:1px}}
.ok{{color:var(--ok);background:var(--ok-soft)}} .warn{{color:var(--warn);background:var(--warn-soft)}}
.bad{{color:var(--bad);background:var(--bad-soft)}} .idle{{color:var(--idle);background:var(--idle-soft)}}
.dot{{width:7px;height:7px;border-radius:50%;background:currentColor;animation:pulse 1.6s ease-in-out infinite}}
@keyframes pulse{{50%{{opacity:.3}}}}
@media (prefers-reduced-motion:reduce){{.dot{{animation:none}}}}
.head{{display:flex;flex-wrap:wrap;align-items:center;gap:4px 8px}}
.title{{font-weight:700;color:var(--ink);text-decoration:none;overflow-wrap:anywhere}} a.title:hover{{text-decoration:underline}}
.chip{{font:500 11.5px/1 var(--mono);color:var(--muted);border:1px solid var(--line);border-radius:4px;padding:3px 6px;white-space:nowrap}}
.chip.new{{color:var(--accent);border-color:var(--accent)}}
.doing,.recent{{margin-top:3px;overflow-wrap:anywhere}} .recent{{color:var(--muted)}}
.next{{margin-top:3px;color:var(--warn)}}
.meta{{margin-top:5px !important;display:flex;flex-wrap:wrap;gap:2px 0;color:var(--muted);font-size:13px;font-variant-numeric:tabular-nums}}
.meta>span+span::before{{content:"·";margin:0 7px;opacity:.6}}
.meta code{{font:500 12.5px var(--mono);color:var(--ink);background:var(--idle-soft);padding:1px 5px;border-radius:3px;overflow-wrap:anywhere}}
.meta time{{white-space:nowrap}}
details.archived{{margin-top:8px}} summary{{cursor:pointer;color:var(--muted);font-size:13px;padding:6px 2px}}
.empty{{color:var(--muted);padding:14px;border:1px dashed var(--line);border-radius:6px;margin:0}}
.stale{{color:var(--warn);background:var(--warn-soft);padding:2px 9px;border-radius:999px;font-size:12px;font-weight:700}}
footer{{margin-top:48px;color:var(--muted);font-size:13px;border-top:1px solid var(--line);padding-top:14px;line-height:1.6}}
footer code{{font:12.5px var(--mono)}}
@media (max-width:600px){{.wrap{{padding:22px 14px 48px}}.row{{grid-template-columns:1fr;gap:6px}}.counts{{gap:8px 18px}}.counts b{{font-size:21px}}}}
"""

JS = """
(function(){
  var rtf=new Intl.RelativeTimeFormat(undefined,{numeric:'auto'});
  var units=[['year',31536e6],['month',2592e6],['day',864e5],['hour',36e5],['minute',6e4]];
  function rel(d){var diff=d-Date.now();for(var i=0;i<units.length;i++){var u=units[i];
    if(Math.abs(diff)>=u[1]||i===units.length-1)return rtf.format(Math.round(diff/u[1]),u[0]);}}
  document.querySelectorAll('time[datetime]').forEach(function(t){var d=new Date(t.getAttribute('datetime'));
    if(isNaN(d))return;t.title=t.textContent;t.textContent=rel(d);});
  // Phones and tablets: open sessions in the Claude app (claude://code/{id}); keep the web link as a chip.
  var ua=navigator.userAgent, touchMac=navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1;
  if(/Android|iPhone|iPad|iPod/i.test(ua)||touchMac){
    document.querySelectorAll('a.title[data-session]').forEach(function(a){
      var id=a.getAttribute('data-session'); if(!/^session_[A-Za-z0-9]+$/.test(id))return;
      var web=document.createElement('a'); web.className='chip'; web.href=a.href; web.textContent='web';
      a.href='claude://code/'+id; a.after(web);
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
  <p class="sub"><span>Snapshot {snapshot}</span><span>{n_sessions} most recent sessions on this account</span><span id="stale" class="stale" hidden>Snapshot is over 6 hours old</span></p>
  {counts}
</header>
<main>
{needs}
{products}
{routines}
</main>
<footer>Refresh from any Claude Code session on the Agent-tracker repository by running the <code>agent-tracker</code> skill, or let the scheduled Routine republish it. On a phone or tablet, session titles open in the Claude app and the small “web” chip opens claude.ai. Costs are this account’s own usage figures.</footer>
</div>
<script>{js}</script>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sessions", help="saved list_sessions result (JSON or raw tool text)")
    ap.add_argument("--triggers", help="saved list_triggers result (JSON or raw tool text)")
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "dashboard.html"))
    args = ap.parse_args()
    sessions, triggers, source = load_records(args)
    page = render(sessions, triggers, datetime.now(timezone.utc))
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(page)
    print(f"{len(sessions)} sessions, {len(triggers)} routines"
          + (f" (from {source})" if source else "") + f" -> {args.out}")


if __name__ == "__main__":
    main()
