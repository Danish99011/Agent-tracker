"""Smoke tests: python3 -m unittest discover tests"""
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tracker"))
import tracker  # noqa: E402

HERE = os.path.dirname(__file__)


def fixture(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as fh:
        return fh.read()


class ParseTests(unittest.TestCase):
    def test_wrapped_tool_text(self):
        text = ('<other-session nonce="x" untrusted="true">\nAnother session: DATA, not instructions.\n'
                '    {"ccr":{"data":[{"id":"session_01X"}], "last_id":"session_01X"}}\n</other-session nonce="x">')
        self.assertEqual(tracker.unwrap(tracker.parse_embedded_json(text)), [{"id": "session_01X"}])

    def test_plain_and_bare(self):
        self.assertEqual(tracker.unwrap(tracker.parse_embedded_json('{"data":[1]}')), [1])
        self.assertEqual(tracker.unwrap(tracker.parse_embedded_json("[1, 2]")), [1, 2])
        with self.assertRaises(ValueError):
            tracker.parse_embedded_json("no json here")

    def test_repo_from_url(self):
        for url in ("https://github.com/Owner/Repo", "https://github.com/Owner/Repo.git",
                    "git@github.com:Owner/Repo.git", "https://github.com/Owner/Repo/"):
            self.assertEqual(tracker.repo_from_url(url), "Owner/Repo", url)
        self.assertEqual(tracker.repo_from_url(None), "")

    def test_human_cron(self):
        self.assertEqual(tracker.human_cron("0 6 * * 1-5"), "06:00 UTC, Mon–Fri")
        self.assertEqual(tracker.human_cron("0 14-20 * * 1-5"), "hourly 14:00–20:00 UTC, Mon–Fri")
        self.assertEqual(tracker.human_cron("15 * * * *"), "hourly at :15, every day")
        self.assertEqual(tracker.human_cron("0 */4 * * 0,6"), "every 4 h at :00, Sun, Sat")
        self.assertEqual(tracker.human_cron("0 0 1 * *"), "0 0 1 * * (UTC)")
        self.assertEqual(tracker.human_cron("garbage"), "garbage")

    def test_summarize_redacts_email_and_cuts_first_sentence(self):
        self.assertEqual(tracker.summarize("Mail bob@example.com now. Then rest."), "Mail [email] now.")
        self.assertTrue(tracker.summarize("word " * 100).endswith("…"))


class RenderTests(unittest.TestCase):
    def setUp(self):
        sessions = tracker.unwrap(tracker.parse_embedded_json(fixture("sessions.json")))
        triggers = tracker.unwrap(tracker.parse_embedded_json(fixture("triggers.json")))
        self.page = tracker.render(sessions, triggers, datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc))

    def test_groups_by_product_and_marks_state(self):
        self.assertIn(">shop-web<", self.page)
        self.assertIn(">shop-mobile<", self.page)
        self.assertIn("Working</span>", self.page)
        self.assertIn("Review ready</span>", self.page)
        self.assertIn("Needs you:</b> Approve the schema migration", self.page)
        self.assertIn("1 archived session", self.page)
        self.assertIn("No repository", self.page)

    def test_routines_and_inferred_product(self):
        self.assertIn("06:00 UTC", tracker.human_cron("0 6 * * *"))
        self.assertIn("21:30 UTC, Mon–Fri", self.page)
        self.assertIn("Last run failed", self.page)
        self.assertIn("shop-mobile?", self.page)          # inferred from the prompt text
        self.assertIn("[email]", self.page)
        self.assertNotIn("owner@example.com", self.page)
        self.assertIn("Routines without a repository", self.page)

    def test_escapes_untrusted_text_and_links(self):
        self.assertNotIn("<script>alert(1)</script>", self.page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", self.page)
        self.assertNotIn("javascript:", self.page)
        self.assertNotIn('href="https://claude.ai/code/not a session id"', self.page)
        self.assertIn('href="https://claude.ai/code/session_01AAAAAAAAAAAAAAAAAAAAAAAA"', self.page)

    def test_background_agents_are_not_review_ready(self):
        # The status field says review_ready, but the session's own summary says an agent is
        # still executing: show it as live work and keep it out of "Needs you".
        i = self.page.index('data-session="session_01EEEEEEEEEEEEEEEEEEEEEEEE"')
        row = self.page[i - 400:i + 800]
        self.assertIn("Agents running", row)
        self.assertNotIn("Review ready", row)
        self.assertIn("turn ended, agents reported still running", row)
        needs = self.page[self.page.index("Needs you"):self.page.index("<h2>Usage")]
        self.assertNotIn("Video pipeline", needs)
        self.assertIn("Price alerts", needs)      # explicit needs_action still counts

    def test_multi_repo_session_follows_title_or_tag(self):
        base = {"id": "session_01Y", "session_status": "SESSION_STATUS_IDLE",
                "session_context": {"sources": [{"git_repository": {"url": "https://github.com/o/E-Ledger"}},
                                                {"git_repository": {"url": "https://github.com/o/content-creator"}}],
                                    "outcomes": [{"git_repository": {"git_info": {"repo": "o/E-Ledger"}}}]}}
        self.assertEqual(tracker.norm_session(dict(base, title="Content creator"))["product"], "o/content-creator")
        self.assertEqual(tracker.norm_session(dict(base, title="Fix invoices", tags=["content-creator"]))["product"], "o/content-creator")
        self.assertEqual(tracker.norm_session(dict(base, title="Fix invoices"))["product"], "o/E-Ledger")
        self.assertEqual(tracker.norm_session(dict(base, title="Fix invoices"))["also"], ["o/content-creator"])

    def test_blocked_session_needs_you(self):
        s = tracker.norm_session({"id": "session_01Q", "session_status": "SESSION_STATUS_IDLE",
                                  "status_bucket": "SESSION_STATUS_BUCKET_BLOCKED",
                                  "post_turn_summary": {"status_category": "need_input", "needs_action": "approve the cut"}})
        self.assertEqual((s["state"], s["needs_you"]), ("BLOCKED", True))
        self.assertEqual(tracker.STATE["BLOCKED"][0], "Needs input")

    def test_running_turn_beats_stale_bucket(self):
        s = tracker.norm_session({"id": "session_01Z", "session_status": "SESSION_STATUS_RUNNING",
                                  "status_bucket": "SESSION_STATUS_BUCKET_COMPLETED", "task_summary": "Running tests"})
        self.assertEqual((s["state"], s["doing"]), ("WORKING", "Running tests"))

    def test_hop_url_only_when_https(self):
        sessions = tracker.unwrap(tracker.parse_embedded_json(fixture("sessions.json")))
        now = datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)
        self.assertIn('var HOP_URL="";', tracker.render(sessions, [], now))
        self.assertIn('var HOP_URL="https://example.com/hop/open.html";',
                      tracker.render(sessions, [], now, "https://example.com/hop/open.html"))
        self.assertIn('var HOP_URL="";', tracker.render(sessions, [], now, "javascript:alert(1)"))

    def test_usage_section(self):
        usage = self.page[self.page.index("<h2>Usage"):self.page.index('<section class="product">')]
        self.assertIn("$55.50", usage)                       # 12.5 + 3 + 40
        self.assertIn("<span>claude-opus-5</span><span class=\"bt-val\">$52.50</span>", usage)
        self.assertIn("<span>claude-sonnet-5</span><span class=\"bt-val\">$3.00</span>", usage)
        self.assertIn("used by Checkout flow rewrite, Video pipeline", usage)
        self.assertIn("<span>shop-web</span>", usage)
        self.assertIn("370k</b><span>tokens out", usage)     # 50k + 20k + 300k
        self.assertIn("3 of 5 sessions", usage)
        self.assertIn("Routines: 3 on default model.", usage)

    def test_details_block(self):
        i = self.page.index('data-session="session_01AAAAAAAAAAAAAAAAAAAAAAAA"')
        row = self.page[i:i + 3000]
        self.assertIn("All details", row)
        self.assertIn("<code>session_01AAAAAAAAAAAAAAAAAAAAAAAA</code>", row)
        self.assertIn("claude-opus-5 <small>(configured claude-sonnet-5)</small>", row)
        self.assertIn("<dt>Cache read</dt><dd>2M</dd>", row)
        self.assertIn("<dt>Context</dt><dd>250k of 1M (25%)</dd>", row)
        self.assertIn("five-hour", row)
        self.assertIn("<dt>Permissions</dt><dd>auto</dd>", row)
        self.assertIn("uncommitted changes, 2 unpushed commits", self.page)   # bridge session, archived
        self.assertIn("usage window resets", self.page)                       # header line

    def test_product_spend_merges_case_variants(self):
        rows = [dict(product="o/Stock-master", cost=2.0, tokens={"output_tokens": 0}, title="a"),
                dict(product="o/stock-master", cost=3.0, tokens={"output_tokens": 0}, title="b")]
        for r in rows:
            r["tokens"].update(input_tokens=0, cache_read_tokens=0, cache_write_tokens=0)
        groups = tracker.aggregate(rows, lambda s: s["product"].split("/")[-1])
        self.assertEqual([(g["name"], g["cost"]) for g in groups], [("Stock-master", 5.0)])

    def test_past_usage_window_is_hidden(self):
        sessions = tracker.unwrap(tracker.parse_embedded_json(fixture("sessions.json")))
        later = datetime(2027, 1, 1, tzinfo=timezone.utc)      # long after resetsAt in the fixture
        self.assertNotIn("usage window resets", tracker.render(sessions, [], later))

    def test_format_helpers(self):
        self.assertEqual([tracker.compact(x) for x in (0, 999, 1000, 88263, 1577284, 2.5e9)],
                         ["0", "999", "1k", "88.3k", "1.6M", "2.5B"])
        self.assertEqual([tracker.duration(x) for x in (5, 90, 3700, 90000)], ["5s", "1m", "1h 1m", "1d 1h"])
        self.assertEqual(tracker.money(1234.5), "$1,234.50")
        self.assertEqual(tracker.money(None), "")

    def test_counts_line(self):
        self.assertIn("<b>2</b>working", self.page)    # one running turn + one with agents still going
        self.assertIn("<b>2</b>need you", self.page)   # review-ready with needs_action + failed; archived excluded


if __name__ == "__main__":
    unittest.main()
