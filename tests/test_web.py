"""The browser UI, driven through Flask's test client.

The same safety rules apply here as in the terminal: nothing is allocated
until Send is pressed, and nothing counts as sent until the operator
confirms. There is also a parity test, because two front ends that disagree
about what gets sent would be worse than having only one.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reddit_promoter import actions, config, db, engine, store   # noqa: E402
from reddit_promoter.senders import RecordingSender              # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "t.db"

        # Point the whole package at a throwaway database.
        patcher = mock.patch.object(config, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher2 = mock.patch.object(db, "DB_PATH", self.db_path)
        patcher2.start()
        self.addCleanup(patcher2.stop)

        conn = db.connect(self.db_path)
        db.init_db(conn)
        self.cfg = config.load_app_config("sleepbound")
        store.register_app(conn, self.cfg)
        store.import_codes(conn, self.cfg)
        conn.close()

        from reddit_promoter import web
        self.app = web.create_app("sleepbound")
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def conn(self):
        c = db.connect(self.db_path)
        self.addCleanup(c.close)
        return c

    def add(self, names="alice"):
        return self.client.post("/add", data={"names": names},
                                follow_redirects=True)

    def only_item(self):
        c = self.conn()
        rows = store.pending_items(c, "sleepbound")
        self.assertEqual(len(rows), 1, "expected exactly one queued item")
        return rows[0]

    def weekly_used(self):
        return store.pool_counts(self.conn(), "sleepbound")["weekly"]["used"]


class TestPage(Base):
    def test_the_page_loads(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"promo codes", r.data)

    def test_it_shows_how_many_codes_are_left(self):
        remaining = store.pool_counts(self.conn(), "sleepbound")["weekly"]["remaining"]
        r = self.client.get("/")
        self.assertIn(str(remaining).encode(), r.data)

    def test_adding_people_queues_them(self):
        # Commas, "u/" prefixes and newlines all appear when pasting from
        # a browser.
        self.add("alice, u/bob\ncarol")
        rows = store.pending_items(self.conn(), "sleepbound")
        self.assertEqual({r["username"] for r in rows}, {"alice", "bob", "carol"})

    def test_junk_pasted_in_is_rejected_not_turned_into_users(self):
        r = self.add("alice  http://reddit.com/u/bob  x  !!")
        rows = store.pending_items(self.conn(), "sleepbound")
        self.assertEqual({r["username"] for r in rows}, {"alice"})
        self.assertIn(b"Ignored", r.data)

    def test_a_repeat_asker_is_flagged_not_given_a_second_code(self):
        self.add("alice")
        item = self.only_item()
        self.client.post(f"/prepare/{item['id']}", follow_redirects=True)
        self.client.post(f"/confirm/{item['id']}", follow_redirects=True)

        self.add("alice")
        rows = [r for r in store.pending_items(self.conn(), "sleepbound")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], engine.DUPLICATE_QUERY)


class TestNothingBurnedEarly(Base):
    def test_adding_allocates_nothing(self):
        self.add("alice,bob,carol")
        self.assertEqual(self.weekly_used(), 0)

    def test_loading_the_page_allocates_nothing(self):
        self.add("alice")
        for _ in range(3):
            self.client.get("/")
        self.assertEqual(self.weekly_used(), 0)

    def test_pressing_send_allocates_but_does_not_mark_sent(self):
        self.add("alice")
        item = self.only_item()
        self.client.post(f"/prepare/{item['id']}", follow_redirects=True)

        self.assertEqual(self.weekly_used(), 1)
        row = store.get_queue_item(self.conn(), item["id"])
        self.assertEqual(row["status"], store.AWAITING_CONFIRM)
        self.assertEqual(store.get_user(self.conn(), "sleepbound", "alice")["state"],
                         store.NEW)

    def test_confirming_records_it(self):
        self.add("alice")
        item = self.only_item()
        self.client.post(f"/prepare/{item['id']}", follow_redirects=True)
        self.client.post(f"/confirm/{item['id']}", follow_redirects=True)

        row = store.get_queue_item(self.conn(), item["id"])
        self.assertEqual(row["status"], store.SENT)
        user = store.get_user(self.conn(), "sleepbound", "alice")
        self.assertEqual(user["state"], store.AWAITING_PROOF)
        self.assertEqual(user["weekly_code"], row["allocated_code"])

    def test_saying_no_keeps_the_code_reserved(self):
        self.add("alice")
        item = self.only_item()
        self.client.post(f"/prepare/{item['id']}", follow_redirects=True)
        code = store.get_queue_item(self.conn(), item["id"])["allocated_code"]
        self.client.post(f"/unconfirm/{item['id']}", follow_redirects=True)

        row = store.get_queue_item(self.conn(), item["id"])
        self.assertEqual(row["status"], store.NEEDS_RETRY)
        self.assertEqual(row["allocated_code"], code)
        self.assertEqual(store.get_user(self.conn(), "sleepbound", "alice")["state"],
                         store.NEW)
        holder = self.conn().execute(
            "SELECT used_by FROM codes WHERE code = ?", (code,)).fetchone()[0]
        self.assertEqual(holder, "alice")

    def test_retrying_after_no_reuses_the_same_code(self):
        self.add("alice")
        item = self.only_item()
        self.client.post(f"/prepare/{item['id']}", follow_redirects=True)
        first = store.get_queue_item(self.conn(), item["id"])["allocated_code"]
        self.client.post(f"/unconfirm/{item['id']}", follow_redirects=True)

        self.client.post(f"/prepare/{item['id']}", follow_redirects=True)
        second = store.get_queue_item(self.conn(), item["id"])["allocated_code"]
        self.assertEqual(first, second)
        self.assertEqual(self.weekly_used(), 1)

    def test_dropping_releases_a_reserved_code(self):
        self.add("alice")
        item = self.only_item()
        self.client.post(f"/prepare/{item['id']}", follow_redirects=True)
        self.assertEqual(self.weekly_used(), 1)

        self.client.post(f"/drop/{item['id']}", follow_redirects=True)
        self.assertEqual(self.weekly_used(), 0)
        self.assertEqual(store.get_queue_item(self.conn(), item["id"])["status"],
                         store.DROPPED)

    def test_an_abandoned_item_comes_back(self):
        """Closing the browser mid-send must not lose the person."""
        self.add("alice")
        item = self.only_item()
        self.client.post(f"/prepare/{item['id']}", follow_redirects=True)
        # No confirm, no unconfirm - just walk away.
        still_there = store.pending_items(self.conn(), "sleepbound")
        self.assertEqual([r["id"] for r in still_there], [item["id"]])


class TestMessages(Base):
    def test_the_message_contains_the_allocated_code(self):
        self.add("alice")
        item = self.only_item()
        self.client.post(f"/prepare/{item['id']}", follow_redirects=True)
        row = store.get_queue_item(self.conn(), item["id"])
        msgs = actions.rendered_messages(self.conn(), self.cfg, row)
        self.assertIn(row["allocated_code"], msgs[0].body)

    def test_the_public_reply_never_contains_a_code(self):
        self.add("alice")
        item = self.only_item()
        self.client.post(f"/prepare/{item['id']}", follow_redirects=True)
        row = store.get_queue_item(self.conn(), item["id"])
        msgs = actions.rendered_messages(self.conn(), self.cfg, row)
        public = [m for m in msgs if m.kind == "comment_reply"]
        self.assertTrue(public)
        for m in public:
            self.assertNotIn(row["allocated_code"], m.body)

    def test_the_compose_link_is_prefilled(self):
        self.add("alice")
        item = self.only_item()
        self.client.post(f"/prepare/{item['id']}", follow_redirects=True)
        row = store.get_queue_item(self.conn(), item["id"])
        pm = actions.rendered_messages(self.conn(), self.cfg, row)[0]
        self.assertIn("reddit.com/message/compose", pm.url)
        self.assertIn("to=alice", pm.url)

    def test_redrawing_the_page_does_not_allocate_again(self):
        self.add("alice")
        item = self.only_item()
        self.client.post(f"/prepare/{item['id']}", follow_redirects=True)
        used = self.weekly_used()
        for _ in range(3):
            self.client.get("/")
        self.assertEqual(self.weekly_used(), used)


class TestParityWithTerminal(Base):
    """Both front ends must produce the same messages for the same item."""

    def test_weekly_code_matches(self):
        self.add("alice")
        item = self.only_item()

        # What the browser would hand over.
        self.client.post(f"/prepare/{item['id']}", follow_redirects=True)
        row = store.get_queue_item(self.conn(), item["id"])
        web_bodies = [m.body for m in
                      actions.rendered_messages(self.conn(), self.cfg, row)]

        # What the terminal would send, from an identical starting point.
        conn2 = db.connect(Path(self.tmp.name) / "t2.db")
        self.addCleanup(conn2.close)
        db.init_db(conn2)
        store.register_app(conn2, self.cfg)
        store.import_codes(conn2, self.cfg)
        from reddit_promoter.sources.base import IncomingComment
        qid = engine.process_comment(
            conn2, self.cfg,
            IncomingComment(item_id="t1_x", post_id="p", author="alice",
                            body="code", permalink="", is_top_level=True,
                            parent_id="p", created_utc=0.0), "me")
        # Force the same public wording, which is chosen at random.
        store.update_ack(conn2, qid, row["ack_body"])
        conn2.commit()
        sender = RecordingSender()
        actions.send_item(conn2, self.cfg, store.get_queue_item(conn2, qid),
                          sender)
        cli_bodies = [s.body for s in sender.sent]

        self.assertEqual(web_bodies, cli_bodies)


if __name__ == "__main__":
    unittest.main(verbosity=2)
