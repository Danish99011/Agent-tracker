# Agent Tracker

One page that answers, for every product you have Claude Code working on:
which sessions are active, what each one is doing right now, what is waiting on
you, and which Routines are scheduled to run next.

It is built for someone running many Claude Code sessions from a phone, a tablet
and the web at once. Sessions are the agents; Routines are the scheduled agents.

## What the page shows

- **Needs you**: sessions that are review-ready or failed, sessions whose last
  summary names an action for you, and Routines whose last run failed.
- **One section per product** (GitHub repository), most recently active first:
  - every open session with its state (Working, Agents running, Review ready,
    Done, Failed), current task or last summary, branch, model, device it was started from,
    context used, cost so far, published artifacts, and the session it continues;
  - the product's Routines with a plain-language schedule, next run, and last
    run result;
  - archived sessions folded away under a disclosure.
- **Routines without a repository**, when a Routine pins no source repo and its
  prompt does not name a known product.
- **Usage**: total spend, tokens out, tokens in and cached, and models in use,
  then spend broken down by model (with the sessions each model serves) and by
  product. Each product heading also carries its own spend.
- **All details** under every session: session id, start and last activity,
  API state, model served and configured, effort, permission mode, where it runs,
  the device it was started from, repositories, tags, the session it continues,
  input, output and cache tokens, cost, context used, the account's usage window,
  Claude Code version, working-tree state for Remote Control sessions, artifacts.
- The header shows when the account's current usage window resets.

A session whose turn has ended but whose own summary says an agent is still
executing is shown as **Agents running**, not Review ready, and stays out of
"Needs you". The Remote API only exposes the turn state, so this reading comes
from the session's summary text and is labelled as such.

A session with several repositories attached is filed under the one its tag or
title names (a session called "Content creator" with E-Ledger attached first
belongs to content-creator); otherwise under its output repository, then the
first one attached. The other repositories show as "also" chips.

Session titles link to claude.ai. The artifact viewer in the Claude mobile app
only lets ordinary web links out, so an app link on the page itself does nothing
there. Instead the titles go through a hop page that hands off to the Claude app
(an Android intent link, or the iOS `claude://code/{session-id}` link) and offers
the web session as a fallback. The session id travels in the URL fragment, so the
host never sees it.

`hop/open.html` is served by GitHub Pages for this repository, and
`tracker/HOP_URL` holds its address. Pages currently deploys from the
`claude/agent-visibility-sessions-yh9ya4` branch; after merging, switch the Pages
source to `main` in Settings → Pages — the published address does not change. If
`tracker/HOP_URL` is absent or is not an `https://` URL, titles simply keep the
claude.ai link.

Times show as "3 hours ago" in the viewer's timezone and as absolute UTC on hover.
A "snapshot is over 6 hours old" badge appears when the page has gone stale.

## Refresh it

From any Claude Code session that has this repository checked out:

    /agent-tracker

The skill calls the Claude Code Remote tools `list_sessions` and `list_triggers`,
runs the renderer, and republishes the page at the URL stored in
`tracker/ARTIFACT_URL`. The first run creates the artifact and commits the URL.

## Schedule it

Ask any Claude Code session to create a Routine, for example:

> Create a Routine named "Agent Tracker refresh" that runs every 4 hours in a fresh
> session on Danish99011/Agent-tracker with the prompt: "Read
> .claude/skills/agent-tracker/SKILL.md and follow it exactly."

Every firing is a normal session and costs usage, so pick a cadence you would
actually look at.

## How it works

1. A Claude Code session calls the Remote tools; their results land in its transcript.
2. `tracker/tracker.py` reads those results and writes `data/dashboard.html`.
3. The session publishes that file as the artifact.

- `tracker/tracker.py` (Python 3.9+, standard library only) finds the newest
  `list_sessions` and `list_triggers` results in `~/.claude/projects/*/*.jsonl`,
  normalises them, and writes one self-contained HTML page. Pass `--sessions` and
  `--triggers` to render from saved JSON instead, `--out` to change the output path.
- `.claude/skills/agent-tracker/SKILL.md` is the refresh procedure a session follows.
- `data/` is gitignored: the snapshot includes cost figures and Routine prompts.

## Limits

- The page covers the 100 most recent sessions on the account.
- The Remote session API does not expose the subagents running inside a session,
  so "which agent" resolves to the session and Routine level. The live task line
  and last summary usually name the role at work ("assembly agent executing EDL").
- Sessions run purely on a local `claude` CLI, without Remote Control, are not
  listed by the API and do not appear.
- Session titles and summaries are written by other sessions and are rendered as
  escaped text; only `https://` artifact links and well-formed session IDs are linked.

## Develop

    python3 -m unittest discover tests
    python3 tracker/tracker.py --sessions tests/fixtures/sessions.json --triggers tests/fixtures/triggers.json --out /tmp/dashboard.html
