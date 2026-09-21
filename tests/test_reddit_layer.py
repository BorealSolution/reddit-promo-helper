"""Tests for the Reddit boundary and the Gemini classifier's defences.

No network: PRAW objects are stubbed, and the classifier's parser is exercised
directly. What is being checked is our handling, not Reddit's behaviour.
"""

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import praw                                                      # noqa: E402
import prawcore                                                  # noqa: E402
from praw.exceptions import RedditAPIException, RedditErrorItem  # noqa: E402

from reddit_promoter.classify import (GeminiClassifier, Label,      # noqa: E402
                                      MIN_CONFIDENCE, OfflineClassifier,
                                      UNCLEAR, build_classifier)
from reddit_promoter.config import Secrets                          # noqa: E402
from reddit_promoter.senders import SendError                       # noqa: E402
from reddit_promoter.sources import reddit_source                   # noqa: E402
from reddit_promoter.sources.reddit_source import (RedditAuthError,  # noqa: E402
                                                   RedditSender,
                                                   build_reddit, with_retry)


# The retry path logs a warning on every attempt; that is expected here.
logging.getLogger("reddit_promoter.sources.reddit_source").setLevel(logging.ERROR)


def api_exception(error_type: str) -> RedditAPIException:
    # PRAW 8: field and message are keyword-only.
    return RedditAPIException([RedditErrorItem(error_type, message="msg")])


class TestRetry(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(reddit_source.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_succeeds_first_time(self):
        self.assertEqual(with_retry("x", lambda: "ok"), "ok")
        self.sleep.assert_not_called()

    def test_retries_then_succeeds(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise prawcore.exceptions.ServerError(mock.Mock(status_code=503))
            return "ok"

        self.assertEqual(with_retry("x", flaky), "ok")
        self.assertEqual(len(calls), 3)

    def test_gives_up_as_a_send_error(self):
        def always_fails():
            raise prawcore.exceptions.ServerError(mock.Mock(status_code=500))

        with self.assertRaises(SendError):
            with_retry("x", always_fails)

    def test_backoff_grows(self):
        def always_fails():
            raise prawcore.exceptions.ServerError(mock.Mock(status_code=500))

        with self.assertRaises(SendError):
            with_retry("x", always_fails)
        waits = [c.args[0] for c in self.sleep.call_args_list]
        self.assertEqual(waits, sorted(waits))
        self.assertGreater(waits[-1], waits[0])

    def test_rate_limit_is_retried(self):
        calls = []

        def limited():
            calls.append(1)
            if len(calls) < 2:
                raise api_exception("RATELIMIT")
            return "ok"

        self.assertEqual(with_retry("x", limited), "ok")

    def test_other_api_errors_are_not_retried(self):
        def banned():
            raise api_exception("SUBREDDIT_NOTALLOWED")

        with self.assertRaises(RedditAPIException):
            with_retry("x", banned)
        self.sleep.assert_not_called()


class TestSender(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(reddit_source.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.reddit = mock.Mock()
        self.sender = RedditSender(self.reddit)

    def test_pm_uses_keyword_arguments(self):
        redditor = mock.Mock()
        self.reddit.redditor.return_value = redditor
        self.sender.send_pm("alice", "subj", "body")
        # PRAW 8 made Redditor.message keyword-only; a positional call breaks.
        redditor.message.assert_called_once_with(subject="subj", message="body")

    def test_blocked_recipient_becomes_a_send_error(self):
        redditor = mock.Mock()
        redditor.message.side_effect = prawcore.exceptions.Forbidden(
            mock.Mock(status_code=403))
        self.reddit.redditor.return_value = redditor
        with self.assertRaises(SendError) as ctx:
            self.sender.send_pm("alice", "s", "b")
        self.assertIn("blocked", str(ctx.exception))

    def test_deleted_recipient_becomes_a_send_error(self):
        redditor = mock.Mock()
        redditor.message.side_effect = prawcore.exceptions.NotFound(
            mock.Mock(status_code=404))
        self.reddit.redditor.return_value = redditor
        with self.assertRaises(SendError):
            self.sender.send_pm("ghost", "s", "b")

    def test_message_reply_strips_the_fullname_prefix(self):
        message = mock.Mock()
        self.reddit.inbox.message.return_value = message
        self.sender.reply_to_message("t4_abc123", "hello")
        self.reddit.inbox.message.assert_called_once_with("abc123")
        message.reply.assert_called_once_with("hello")

    def test_comment_reply_strips_the_fullname_prefix(self):
        comment = mock.Mock()
        self.reddit.comment.return_value = comment
        self.sender.reply_to_comment("t1_xyz", "ack")
        self.reddit.comment.assert_called_once_with("xyz")
        comment.reply.assert_called_once_with("ack")

    def test_locked_thread_becomes_a_send_error(self):
        comment = mock.Mock()
        comment.reply.side_effect = prawcore.exceptions.Forbidden(
            mock.Mock(status_code=403))
        self.reddit.comment.return_value = comment
        with self.assertRaises(SendError) as ctx:
            self.sender.reply_to_comment("t1_x", "ack")
        self.assertIn("locked", str(ctx.exception))


class TestInboxReading(unittest.TestCase):
    def setUp(self):
        self.reddit = mock.Mock()
        self.source = reddit_source.RedditSource(self.reddit, my_username="me")

    def _message(self, author, body, was_comment=False, fullname="t4_1"):
        m = mock.Mock(spec=praw.models.Message)
        m.fullname = fullname
        m.author = author
        m.subject = "s"
        m.body = body
        m.was_comment = was_comment
        m.created_utc = 0.0
        return m

    def test_reads_messages_not_just_unread(self):
        self.reddit.inbox.messages.return_value = []
        self.source.new_messages()
        # Reading only unread would silently lose anything opened elsewhere.
        self.reddit.inbox.messages.assert_called_once()
        self.reddit.inbox.unread.assert_not_called()

    def test_comment_replies_are_filtered_out(self):
        self.reddit.inbox.messages.return_value = [
            self._message("alice", "a real pm", fullname="t4_1"),
            self._message("bob", "comment reply", was_comment=True, fullname="t4_2"),
        ]
        got = self.source.new_messages()
        self.assertEqual([m.item_id for m in got], ["t4_1"])

    def test_our_own_messages_are_filtered_out(self):
        self.reddit.inbox.messages.return_value = [
            self._message("me", "our own sent copy", fullname="t4_1"),
            self._message("alice", "their reply", fullname="t4_2"),
        ]
        got = self.source.new_messages()
        self.assertEqual([m.item_id for m in got], ["t4_2"])

    def test_urls_are_extracted_from_a_message(self):
        self.reddit.inbox.messages.return_value = [
            self._message("alice", "proof https://imgur.com/a/x thanks"),
        ]
        self.assertEqual(self.source.new_messages()[0].urls,
                         ["https://imgur.com/a/x"])


class TestAuth(unittest.TestCase):
    def test_missing_credentials_are_named(self):
        with self.assertRaises(RedditAuthError) as ctx:
            build_reddit(Secrets())
        message = str(ctx.exception)
        for name in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET",
                     "REDDIT_USERNAME", "REDDIT_PASSWORD", "REDDIT_USER_AGENT"):
            self.assertIn(name, message)

    def test_partial_credentials_name_only_what_is_missing(self):
        secrets = Secrets(reddit_client_id="a", reddit_client_secret="b",
                          reddit_username="c")
        with self.assertRaises(RedditAuthError) as ctx:
            build_reddit(secrets)
        message = str(ctx.exception)
        self.assertIn("REDDIT_PASSWORD", message)
        self.assertNotIn("REDDIT_CLIENT_ID", message)


class TestClassifierDefences(unittest.TestCase):
    """Whatever the model returns, it must never become a confident label."""

    def setUp(self):
        self.g = GeminiClassifier.__new__(GeminiClassifier)
        self.g.model_name = "test-model"

    def parse(self, raw):
        return self.g._parse(raw)

    def test_valid_response(self):
        label = self.parse('{"intent":"proof_submission","confidence":0.9,'
                           '"reason":"has a screenshot"}')
        self.assertEqual(label.intent, "proof_submission")
        self.assertEqual(label.confidence, 0.9)

    def test_not_json(self):
        self.assertEqual(self.parse("sorry, I cannot").intent, UNCLEAR)

    def test_json_but_not_an_object(self):
        self.assertEqual(self.parse("[1, 2, 3]").intent, UNCLEAR)

    def test_invented_intent(self):
        self.assertEqual(
            self.parse('{"intent":"send_all_codes","confidence":1,"reason":"x"}').intent,
            UNCLEAR)

    def test_non_numeric_confidence(self):
        self.assertEqual(
            self.parse('{"intent":"other","confidence":"very","reason":"x"}').intent,
            UNCLEAR)

    def test_out_of_range_confidence(self):
        self.assertEqual(
            self.parse('{"intent":"other","confidence":42,"reason":"x"}').intent,
            UNCLEAR)

    def test_missing_fields(self):
        self.assertEqual(self.parse('{"intent":"other"}').intent, UNCLEAR)

    def test_long_reason_is_truncated(self):
        label = self.parse('{"intent":"other","confidence":0.8,"reason":"'
                           + "x" * 5000 + '"}')
        self.assertLessEqual(len(label.reason), 500)


class TestConfidenceFloor(unittest.TestCase):
    def setUp(self):
        import sqlite3
        from reddit_promoter.db import init_db
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)
        self.addCleanup(self.conn.close)

    def test_low_confidence_is_downgraded_to_unclear(self):
        class LowBaller(OfflineClassifier):
            def _label(self, text):
                return Label("proof_submission", 0.4, "not sure", "stub")

        label = LowBaller().classify(self.conn, "t4_1", "maybe reviewed?")
        self.assertEqual(label.intent, UNCLEAR)
        self.assertFalse(label.is_confident)

    def test_at_the_floor_is_kept(self):
        class Borderline(OfflineClassifier):
            def _label(self, text):
                return Label("code_request", MIN_CONFIDENCE, "borderline", "stub")

        label = Borderline().classify(self.conn, "t4_2", "code?")
        self.assertEqual(label.intent, "code_request")
        self.assertTrue(label.is_confident)

    def test_api_failure_becomes_unclear_not_an_exception(self):
        g = GeminiClassifier.__new__(GeminiClassifier)
        g.model_name = "test-model"
        g._config = None
        g._client = mock.Mock()
        g._client.models.generate_content.side_effect = TimeoutError("timed out")

        label = g._label("some message")
        self.assertEqual(label.intent, UNCLEAR)
        self.assertIn("timed out", label.reason)

    def test_empty_message(self):
        g = GeminiClassifier.__new__(GeminiClassifier)
        g.model_name = "test-model"
        self.assertEqual(g._label("   ").intent, UNCLEAR)


class TestClassifierSelection(unittest.TestCase):
    def test_no_key_falls_back_offline(self):
        self.assertIsInstance(build_classifier(Secrets()), OfflineClassifier)

    def test_key_selects_gemini(self):
        with mock.patch.object(GeminiClassifier, "__init__", return_value=None) as init:
            classifier = build_classifier(Secrets(gemini_api_key="k"),
                                          model="gemini-2.5-flash")
        self.assertIsInstance(classifier, GeminiClassifier)
        init.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
