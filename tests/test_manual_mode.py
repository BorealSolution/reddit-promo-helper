"""Manual mode: the primary path while Reddit API access is pending.

Covers the whole chain with no Reddit access of any kind - paste in a
comment, see it drafted, approve, copy, confirm sent, state updates.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rich.console import Console                               # noqa: E402

from reddit_promoter import actions, clipboard, engine, store  # noqa: E402
from reddit_promoter.classify import OfflineClassifier         # noqa: E402
from reddit_promoter.config import Secrets, load_app_config    # noqa: E402
from reddit_promoter.db import connect, init_db                # noqa: E402
from reddit_promoter.senders import ManualSender, SendError    # noqa: E402
from reddit_promoter.sources.base import (IncomingComment,     # noqa: E402
                                          IncomingMessage)


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
        self.classifier = OfflineClassifier()
        self.console = Console(file=open(Path(self.tmp.name) / "out.txt", "w",
                                         encoding="utf-8"), width=100)
        self.addCleanup(self.console.file.close)

        # Never touch the real clipboard from a test.
        patcher = mock.patch.object(clipboard, "copy", return_value="stub")
        self.clip = patcher.start()
        self.addCleanup(patcher.stop)

    def manual(self, answers):
        """A ManualSender that answers the confirm prompts from a list."""
        queue = list(answers)
        return ManualSender(self.console,
                            confirm=lambda q: queue.pop(0) if queue else False)

    def add_comment(self, username, body="code", n=1, top=True):
        item = IncomingComment(
            item_id=f"manual_c_{username}_{n}", post_id="manual",
            author=username, body=body, permalink="", is_top_level=top,
            parent_id="manual", created_utc=0.0)
        return engine.process_comment(self.conn, self.cfg, item, "me",
                                      self.classifier)

    def add_message(self, username, body, n=1):
        item = IncomingMessage(
            item_id=f"manual_m_{username}_{n}", author=username,
            subject="(pasted by hand)", body=body, parent_id=None,
            created_utc=0.0)
        return engine.process_message(self.conn, self.cfg, item,
                                      self.classifier, app_id=self.cfg.app_id)

    def row(self, qid):
        return store.get_queue_item(self.conn, qid)

    def weekly_used(self):
        return store.pool_counts(self.conn, "sleepbound")["weekly"]["used"]


class TestNoRedditDependency(Base):
    """Manual mode must run with no Reddit keys whatsoever."""

    def test_empty_secrets_report_reddit_missing_but_gemini_is_separate(self):
        secrets = Secrets(gemini_api_key="only-this-one")
        self.assertTrue(secrets.missing_reddit())
        self.assertEqual(secrets.gemini_api_key, "only-this-one")

    def test_the_whole_chain_runs_without_importing_praw(self):
        # If any of this reached for PRAW it would need credentials.
        qid = self.add_comment("alice")
        sender = self.manual([True, True])
        result = actions.send_item(self.conn, self.cfg, self.row(qid), sender)
        self.assertIsNotNone(result["code"])
        self.assertEqual(store.get_user(self.conn, "sleepbound", "alice")["state"],
                         store.AWAITING_PROOF)

    def test_no_module_in_the_manual_path_imports_reddit_source(self):
        import reddit_promoter.actions as a
        import reddit_promoter.dashboard as d
        import reddit_promoter.engine as e
        import reddit_promoter.senders as s
        import reddit_promoter.store as st
        for module in (a, d, e, s, st):
            src = Path(module.__file__).read_text(encoding="utf-8")
            self.assertNotIn("reddit_source", src,
                             f"{module.__name__} reaches for the Reddit layer")


class TestManualChain(Base):
    def test_a_pasted_comment_becomes_a_weekly_draft(self):
        qid = self.add_comment("alice")
        item = self.row(qid)
        self.assertEqual(item["action"], engine.WEEKLY_CODE)
        self.assertIn(item["preview_code"], item["draft_body"])

    def test_approving_copies_the_exact_text_and_records_the_send(self):
        qid = self.add_comment("alice")
        sender = self.manual([True, True])
        result = actions.send_item(self.conn, self.cfg, self.row(qid), sender)

        copied = [c.args[0] for c in self.clip.call_args_list]
        self.assertEqual(len(copied), 2)            # the PM, then the ack
        self.assertIn(result["code"], copied[0])
        self.assertNotIn(result["code"], copied[1])  # public reply has no code
        self.assertEqual(self.row(qid)["status"], store.SENT)

    def test_the_copied_text_is_what_the_dashboard_showed(self):
        qid = self.add_comment("alice")
        shown_ack = self.row(qid)["ack_body"]
        sender = self.manual([True, True])
        actions.send_item(self.conn, self.cfg, self.row(qid), sender)
        copied = [c.args[0] for c in self.clip.call_args_list]
        self.assertEqual(copied[1], shown_ack)

    def test_duplicate_check_uses_real_state(self):
        qid = self.add_comment("alice", n=1)
        actions.send_item(self.conn, self.cfg, self.row(qid),
                          self.manual([True, True]))
        qid2 = self.add_comment("alice", body="again?", n=2)
        self.assertEqual(self.row(qid2)["action"], engine.DUPLICATE_QUERY)

    def test_proof_message_draws_from_the_lifetime_pool(self):
        qid = self.add_message("alice", "reviewed! https://imgur.com/a/x")
        result = actions.send_item(self.conn, self.cfg, self.row(qid),
                                   self.manual([True]))
        pool = self.conn.execute("SELECT pool FROM codes WHERE code = ?",
                                 (result["code"],)).fetchone()[0]
        self.assertEqual(pool, "lifetime")
        self.assertEqual(store.get_user(self.conn, "sleepbound", "alice")["state"],
                         store.LIFETIME_SENT)


class TestNotSent(Base):
    """Answering "no" must never look like a send."""

    def test_answering_no_marks_needs_retry_and_reserves_the_code(self):
        qid = self.add_comment("alice")
        with self.assertRaises(SendError):
            actions.send_item(self.conn, self.cfg, self.row(qid),
                              self.manual([False]))

        item = self.row(qid)
        self.assertEqual(item["status"], store.NEEDS_RETRY)
        self.assertIsNotNone(item["allocated_code"])
        self.assertEqual(store.get_user(self.conn, "sleepbound", "alice")["state"],
                         store.NEW)

    def test_retrying_reuses_the_same_code(self):
        qid = self.add_comment("alice")
        with self.assertRaises(SendError):
            actions.send_item(self.conn, self.cfg, self.row(qid),
                              self.manual([False]))
        reserved = self.row(qid)["allocated_code"]
        used = self.weekly_used()

        result = actions.send_item(self.conn, self.cfg, self.row(qid),
                                   self.manual([True, True]))
        self.assertEqual(result["code"], reserved)
        self.assertEqual(self.weekly_used(), used)

    def test_closed_input_counts_as_not_sent(self):
        """Regression: stdin ending mid-prompt used to leave the item
        pending with a code silently reserved."""
        qid = self.add_comment("alice")

        def eof(_question):
            raise EOFError

        sender = ManualSender(self.console, confirm=eof)
        with self.assertRaises(Exception):
            actions.send_item(self.conn, self.cfg, self.row(qid), sender)

        item = self.row(qid)
        self.assertEqual(item["status"], store.NEEDS_RETRY)
        self.assertNotEqual(item["status"], store.PENDING)

    def test_an_unexpected_crash_still_marks_needs_retry(self):
        """Regression: only SendError was caught, so anything else left the
        item pending while its code was already claimed."""
        qid = self.add_comment("alice")

        class Exploding(ManualSender):
            def send_pm(self, username, subject, body):
                raise RuntimeError("something unexpected")

        with self.assertRaises(RuntimeError):
            actions.send_item(self.conn, self.cfg, self.row(qid),
                              Exploding(self.console))

        item = self.row(qid)
        self.assertEqual(item["status"], store.NEEDS_RETRY)
        self.assertIsNotNone(item["allocated_code"])
        # Reserved for that user, so the retry cannot burn a second code.
        holder = self.conn.execute(
            "SELECT used_by FROM codes WHERE code = ?",
            (item["allocated_code"],)).fetchone()[0]
        self.assertEqual(holder, "alice")

    def test_no_pending_item_ever_holds_an_allocated_code(self):
        """The invariant the bug violated."""
        for i, name in enumerate(["a", "b", "c"], start=1):
            qid = self.add_comment(name, n=i)
            if i == 2:
                with self.assertRaises(SendError):
                    actions.send_item(self.conn, self.cfg, self.row(qid),
                                      self.manual([False]))
            elif i == 3:
                actions.send_item(self.conn, self.cfg, self.row(qid),
                                  self.manual([True, True]))

        bad = self.conn.execute(
            "SELECT id FROM queue WHERE status = ? AND allocated_code IS NOT NULL",
            (store.PENDING,)).fetchall()
        self.assertEqual(bad, [])


class TestNothingBurnedWithoutApproval(Base):
    def test_drafting_allocates_nothing(self):
        for i, name in enumerate(["a", "b", "c"], start=1):
            self.add_comment(name, n=i)
        self.assertEqual(self.weekly_used(), 0)

    def test_skipping_allocates_nothing(self):
        qid = self.add_comment("alice")
        actions.skip_item(self.conn, qid)
        self.assertEqual(self.weekly_used(), 0)
        self.assertEqual(self.row(qid)["status"], store.PENDING)

    def test_dropping_a_reserved_item_releases_the_code(self):
        qid = self.add_comment("alice")
        with self.assertRaises(SendError):
            actions.send_item(self.conn, self.cfg, self.row(qid),
                              self.manual([False]))
        self.assertEqual(self.weekly_used(), 1)

        actions.drop_item(self.conn, qid, self.row(qid))
        self.assertEqual(self.weekly_used(), 0)

    def test_the_previewed_code_is_not_consumed_by_previewing(self):
        qid = self.add_comment("alice")
        preview = self.row(qid)["preview_code"]
        for _ in range(5):
            store.peek_next_code(self.conn, "sleepbound", "weekly")
        self.assertEqual(self.weekly_used(), 0)
        self.assertEqual(store.peek_next_code(self.conn, "sleepbound", "weekly"),
                         preview)


class TestFreeformReply(Base):
    """Answering someone who is stuck."""

    def test_an_undrafted_item_cannot_be_sent(self):
        qid = self.add_message("alice", "how do I redeem this?")
        self.assertEqual(self.row(qid)["action"], engine.REVIEW_ONLY)
        with self.assertRaises(actions.ActionError):
            actions.send_item(self.conn, self.cfg, self.row(qid),
                              self.manual([True]))

    def test_once_written_it_can_be_sent(self):
        """Regression: an edited help reply was refused, so there was no way
        to answer anyone."""
        qid = self.add_message("alice", "how do I redeem this?")
        store.update_draft(self.conn, qid, "Play Store, profile, Redeem code.")
        self.conn.commit()

        result = actions.send_item(self.conn, self.cfg, self.row(qid),
                                   self.manual([True]),
                                   body_override="Play Store, profile, Redeem code.")
        self.assertIsNone(result["code"])
        self.assertEqual(self.row(qid)["status"], store.SENT)

    def test_answering_a_question_changes_no_state_and_burns_no_code(self):
        self.add_comment("alice")                     # she already has a draft
        qid = self.add_message("alice", "how do I redeem this?", n=2)
        before = store.get_user(self.conn, "sleepbound", "alice")["state"]

        store.update_draft(self.conn, qid, "Play Store, profile, Redeem code.")
        self.conn.commit()
        actions.send_item(self.conn, self.cfg, self.row(qid), self.manual([True]),
                          body_override="Play Store, profile, Redeem code.")

        self.assertEqual(store.get_user(self.conn, "sleepbound", "alice")["state"],
                         before)
        self.assertEqual(self.weekly_used(), 0)


class TestClipboard(unittest.TestCase):
    def test_describe_names_a_backend(self):
        self.assertTrue(clipboard.describe())

    def test_copying_nothing_is_an_error(self):
        with self.assertRaises(clipboard.ClipboardUnavailable):
            clipboard.copy("")

    def test_a_dead_clipboard_does_not_stop_the_send(self):
        """The text is printed instead; losing the clipboard must not block
        the workflow."""
        console = Console(file=open(__file__ + ".out", "w", encoding="utf-8"),
                          width=100)
        try:
            with mock.patch.object(
                    clipboard, "copy",
                    side_effect=clipboard.ClipboardUnavailable("no backend")):
                sender = ManualSender(console, confirm=lambda q: True)
                sender.send_pm("alice", "subj", "the body")
            self.assertEqual(len(sender.sent), 1)
        finally:
            console.file.close()
            Path(__file__ + ".out").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
