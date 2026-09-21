"""Tests for recovering codes handed out by hand before the tool existed.

The point of the feature is that a previously-served user must never look
new again, so these check exactly that.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reddit_promoter import backfill, engine, store              # noqa: E402
from reddit_promoter.config import load_app_config               # noqa: E402
from reddit_promoter.db import connect, init_db                  # noqa: E402
from reddit_promoter.sources.base import IncomingComment         # noqa: E402

# Real-shaped codes: 23 uppercase alphanumerics.
POOL_CODE = "AAAAAAAAOLDWEEKLYAAAAAA"      # the first code in Reddit Promo.csv
OLD_CODE = "AAAAAAAAOLDWEEKLYAAAAAA"       # from an earlier batch, not in a CSV
OLD_LIFETIME = "BBBBBBBBOLDLIFETIMEBBBB"


def sent_message(dest, body, subject="Your Sleepbound promo code", when=1000.0):
    m = mock.Mock()
    m.dest = dest
    m.body = body
    m.subject = subject
    m.created_utc = when
    return m


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = connect(Path(self.tmp.name) / "t.db")
        self.addCleanup(self.conn.close)
        init_db(self.conn)
        self.cfg = load_app_config("sleepbound")
        store.register_app(self.conn, self.cfg)
        store.import_codes(self.conn, self.cfg)
        self.reddit = mock.Mock()

    def scan(self, messages):
        self.reddit.inbox.sent.return_value = messages
        return backfill.scan_sent(self.reddit, self.conn, "sleepbound")

    def comment(self, n, author, body):
        return IncomingComment(
            item_id=f"t1_{n}", post_id="p1", author=author, body=body,
            permalink="https://example.com/1", is_top_level=True,
            parent_id="t3_p1", created_utc=0.0)


class TestScanning(Base):
    def test_finds_a_code_and_its_recipient(self):
        report = self.scan([sent_message("alice", f"here you go: {OLD_CODE}")])
        self.assertEqual(len(report.found), 1)
        self.assertEqual(report.found[0].username, "alice")
        self.assertEqual(report.found[0].code, OLD_CODE)
        self.assertEqual(report.found[0].pool, "weekly")

    def test_messages_without_a_code_are_skipped(self):
        report = self.scan([
            sent_message("alice", "thanks for the interest!"),
            sent_message("bob", f"code: {OLD_CODE}"),
        ])
        self.assertEqual(report.skipped_no_code, 1)
        self.assertEqual([f.username for f in report.found], ["bob"])

    def test_lifetime_is_told_apart_from_weekly(self):
        report = self.scan([
            sent_message("alice", f"Here is your lifetime code:\n{OLD_LIFETIME}",
                         subject="Your lifetime Sleepbound code"),
        ])
        self.assertEqual(report.found[0].pool, "lifetime")

    def test_a_user_with_both_codes_yields_both(self):
        report = self.scan([
            sent_message("alice", f"weekly: {OLD_CODE}", when=1.0),
            sent_message("alice", f"your lifetime code {OLD_LIFETIME}",
                         subject="lifetime", when=2.0),
        ])
        self.assertEqual({f.pool for f in report.found}, {"weekly", "lifetime"})

    def test_repeat_sends_keep_the_earliest(self):
        report = self.scan([
            sent_message("alice", f"resend {POOL_CODE}", when=99.0),
            sent_message("alice", f"first {OLD_CODE}", when=1.0),
        ])
        self.assertEqual(len(report.found), 1)
        self.assertEqual(report.found[0].code, OLD_CODE)

    def test_subreddit_messages_are_ignored(self):
        report = self.scan([sent_message("#somesubreddit", f"code {OLD_CODE}")])
        self.assertEqual(report.found, [])

    def test_recognises_a_code_that_is_in_the_csvs(self):
        report = self.scan([sent_message("alice", f"code {POOL_CODE}")])
        self.assertTrue(report.found[0].in_pool)

    def test_recognises_a_code_from_an_older_batch(self):
        report = self.scan([sent_message("alice", f"code {OLD_CODE}")])
        self.assertFalse(report.found[0].in_pool)

    def test_scanning_alone_writes_nothing(self):
        self.scan([sent_message("alice", f"code {POOL_CODE}")])
        self.assertIsNone(store.get_user(self.conn, "sleepbound", "alice"))
        self.assertEqual(
            store.pool_counts(self.conn, "sleepbound")["weekly"]["used"], 0)


class TestApplying(Base):
    def test_a_backfilled_user_is_no_longer_new(self):
        report = self.scan([sent_message("alice", f"code {OLD_CODE}")])
        backfill.apply_backfill(self.conn, "sleepbound", report)

        user = store.get_user(self.conn, "sleepbound", "alice")
        self.assertEqual(user["state"], store.AWAITING_PROOF)
        self.assertEqual(user["weekly_code"], OLD_CODE)

    def test_a_backfilled_user_asking_again_gets_a_duplicate_query(self):
        """The whole reason the feature exists."""
        report = self.scan([sent_message("alice", f"code {OLD_CODE}")])
        backfill.apply_backfill(self.conn, "sleepbound", report)

        qid = engine.process_comment(self.conn, self.cfg,
                                     self.comment(1, "alice", "code please"),
                                     "me")
        item = store.get_queue_item(self.conn, qid)
        self.assertEqual(item["action"], engine.DUPLICATE_QUERY)
        self.assertIsNone(item["pool"])

    def test_without_backfill_they_would_have_been_served_again(self):
        # The counterfactual, so the test above cannot silently stop meaning
        # anything.
        qid = engine.process_comment(self.conn, self.cfg,
                                     self.comment(1, "alice", "code please"),
                                     "me")
        self.assertEqual(store.get_queue_item(self.conn, qid)["action"],
                         engine.WEEKLY_CODE)

    def test_a_code_from_the_csvs_is_retired(self):
        report = self.scan([sent_message("alice", f"code {POOL_CODE}")])
        backfill.apply_backfill(self.conn, "sleepbound", report)

        row = self.conn.execute(
            "SELECT used_by FROM codes WHERE code = ?", (POOL_CODE,)).fetchone()
        self.assertEqual(row[0], "alice")
        self.assertEqual(store.peek_next_code(self.conn, "sleepbound", "weekly")
                         != POOL_CODE, True)

    def test_a_code_from_an_older_batch_does_not_touch_the_pool(self):
        report = self.scan([sent_message("alice", f"code {OLD_CODE}")])
        backfill.apply_backfill(self.conn, "sleepbound", report)
        self.assertEqual(
            store.pool_counts(self.conn, "sleepbound")["weekly"]["used"], 0)

    def test_applying_twice_is_idempotent(self):
        messages = [sent_message("alice", f"code {POOL_CODE}")]
        backfill.apply_backfill(self.conn, "sleepbound", self.scan(messages))
        used_after_first = store.pool_counts(
            self.conn, "sleepbound")["weekly"]["used"]

        backfill.apply_backfill(self.conn, "sleepbound", self.scan(messages))
        self.assertEqual(
            store.pool_counts(self.conn, "sleepbound")["weekly"]["used"],
            used_after_first)

    def test_a_lifetime_user_is_not_downgraded(self):
        report = self.scan([
            sent_message("alice", f"lifetime code {OLD_LIFETIME}",
                         subject="lifetime", when=1.0)])
        backfill.apply_backfill(self.conn, "sleepbound", report)
        self.assertEqual(store.get_user(self.conn, "sleepbound", "alice")["state"],
                         store.LIFETIME_SENT)

        later = self.scan([sent_message("alice", f"weekly {OLD_CODE}", when=2.0)])
        backfill.apply_backfill(self.conn, "sleepbound", later)
        self.assertEqual(store.get_user(self.conn, "sleepbound", "alice")["state"],
                         store.LIFETIME_SENT)

    def test_a_blocked_user_stays_blocked(self):
        from reddit_promoter import actions
        actions.block_user(self.conn, "sleepbound", "spammer")
        report = self.scan([sent_message("spammer", f"code {OLD_CODE}")])
        backfill.apply_backfill(self.conn, "sleepbound", report)
        self.assertEqual(store.get_user(self.conn, "sleepbound", "spammer")["state"],
                         store.BLOCKED)

    def test_a_code_attributed_to_someone_else_is_reported_not_stolen(self):
        from reddit_promoter.db import transaction
        with transaction(self.conn):
            store.ensure_user(self.conn, "sleepbound", "first")
            store.allocate_code(self.conn, "sleepbound", "weekly", "first")

        report = self.scan([sent_message("second", f"code {POOL_CODE}")])
        self.assertTrue(report.conflicts)

        backfill.apply_backfill(self.conn, "sleepbound", report)
        row = self.conn.execute(
            "SELECT used_by FROM codes WHERE code = ?", (POOL_CODE,)).fetchone()
        self.assertEqual(row[0], "first")


class TestManualMarking(Base):
    def test_usernames_are_normalised(self):
        self.assertEqual(
            backfill.normalise_usernames(["alice", "u/bob", "/u/carol", " dave "]),
            ["alice", "bob", "carol", "dave"])

    def test_duplicates_are_collapsed(self):
        self.assertEqual(backfill.normalise_usernames(["alice", "u/alice"]),
                         ["alice"])

    def test_empty_entries_are_dropped(self):
        self.assertEqual(backfill.normalise_usernames(["", "  ", "u/"]), [])

    def test_marking_prevents_a_second_code(self):
        backfill.mark_users_served(self.conn, "sleepbound", ["u/alice"])
        qid = engine.process_comment(self.conn, self.cfg,
                                     self.comment(1, "alice", "code please"),
                                     "me")
        self.assertEqual(store.get_queue_item(self.conn, qid)["action"],
                         engine.DUPLICATE_QUERY)

    def test_marking_burns_no_codes(self):
        backfill.mark_users_served(self.conn, "sleepbound", ["alice", "bob"])
        self.assertEqual(
            store.pool_counts(self.conn, "sleepbound")["weekly"]["used"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
