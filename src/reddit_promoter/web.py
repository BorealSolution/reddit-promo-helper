"""A local web UI for the review queue.

Runs on this machine only, talks to the same SQLite database and the same
engine/actions as the terminal dashboard. Sending still happens in Reddit,
by the operator - this just removes the typing:

  1. paste usernames, press Add
  2. press Send: the message is copied and Reddit opens with it filled in
  3. press "Yes, sent" (or "No") when you come back

No code is allocated until step 2, and nothing counts as sent until step 3.
"""

from __future__ import annotations

import re
import threading
import time
import webbrowser

from flask import Flask, redirect, render_template_string, request, url_for

from . import actions, backfill, engine, store
from .classify import build_classifier
from .config import discover_app_ids, load_app_config, load_secrets
from .db import backup_db, connect, ensure_data_dirs, init_db
from .sources.base import IncomingComment, IncomingMessage, extract_urls

PAGE = """
<!doctype html>
<title>{{ app_name }} promo codes</title>
<style>
  :root {
    --bg:#f6f7f9; --card:#fff; --ink:#1a1d21; --muted:#6b7280;
    --line:#e3e6ea; --accent:#2f6feb; --good:#17803d; --warn:#b45309;
    --bad:#b42318; --shadow:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.1);
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#14161a; --card:#1c1f24; --ink:#e8eaed; --muted:#9aa2ad;
            --line:#2c3138; --accent:#6f9bff; --good:#4ade80; --warn:#fbbf24;
            --bad:#f87171; --shadow:none; }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5
         system-ui,-apple-system,"Segoe UI",sans-serif; }
  .wrap { max-width:840px; margin:0 auto; padding:24px 16px 64px; }
  h1 { font-size:20px; margin:0 0 4px; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:20px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:16px; margin-bottom:14px; box-shadow:var(--shadow); }
  .stats { display:flex; gap:20px; flex-wrap:wrap; }
  .stat b { font-size:22px; display:block; font-weight:650; }
  .stat span { color:var(--muted); font-size:12px; text-transform:uppercase;
               letter-spacing:.04em; }
  .low b { color:var(--warn); } .empty b { color:var(--bad); }
  textarea, input[type=text] { width:100%; padding:10px; border:1px solid var(--line);
      border-radius:8px; background:var(--bg); color:var(--ink); font:inherit; }
  textarea { min-height:70px; resize:vertical; }
  button { font:inherit; font-weight:550; padding:9px 16px; border-radius:8px;
           border:1px solid var(--line); background:var(--card); color:var(--ink);
           cursor:pointer; }
  button:hover { border-color:var(--accent); }
  .primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  .danger { color:var(--bad); }
  .row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  .who { font-weight:650; }
  .tag { font-size:11px; padding:2px 8px; border-radius:99px; border:1px solid var(--line);
         color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
  .tag.code { color:var(--accent); border-color:var(--accent); }
  .tag.dup { color:var(--warn); border-color:var(--warn); }
  .tag.help { color:var(--bad); border-color:var(--bad); }
  .said { border-left:3px solid var(--line); padding:2px 0 2px 12px; margin:10px 0;
          color:var(--muted); white-space:pre-wrap; }
  .msg { background:var(--bg); border:1px solid var(--line); border-radius:8px;
         padding:12px; white-space:pre-wrap; font-size:14px; margin:8px 0; }
  .hist { font-size:13px; color:var(--muted); margin:2px 0 8px; }
  .step { display:flex; gap:10px; align-items:flex-start; margin-bottom:10px; }
  .n { flex:0 0 22px; height:22px; border-radius:99px; background:var(--accent);
       color:#fff; font-size:12px; font-weight:700; display:grid; place-items:center;
       margin-top:2px; }
  a { color:var(--accent); }
  .empty-state { text-align:center; color:var(--muted); padding:28px; }
  .flash { border-left:3px solid var(--good); padding-left:12px; margin-bottom:14px;
           color:var(--good); }
  .warnbox { border-left:3px solid var(--warn); padding-left:12px; color:var(--warn);
             margin-bottom:14px; font-size:14px; }
</style>

<div class="wrap">
  <h1>{{ app_name }} promo codes</h1>
  <div class="sub">r/{{ subreddit }} &middot; sending is manual &mdash; nothing
    leaves this machine on its own</div>

  {% if flash %}<div class="flash">{{ flash }}</div>{% endif %}
  {% for w in warnings %}<div class="warnbox">{{ w }}</div>{% endfor %}

  <div class="card stats">
    <div class="stat {{ 'empty' if weekly==0 else ('low' if weekly<weekly_min else '') }}">
      <b>{{ weekly }}</b><span>weekly codes left</span></div>
    <div class="stat {{ 'empty' if lifetime==0 else ('low' if lifetime<life_min else '') }}">
      <b>{{ lifetime }}</b><span>lifetime codes left</span></div>
    <div class="stat"><b>{{ people }}</b><span>people on record</span></div>
    <div class="stat"><b>{{ items|length }}</b><span>waiting for you</span></div>
  </div>

  <div class="card">
    <form method="post" action="{{ url_for('add') }}">
      <label for="names"><b>Someone asked for a code</b></label>
      <div class="sub" style="margin:4px 0 8px">Paste one or more usernames &mdash;
        commas, spaces or new lines all work.</div>
      <textarea id="names" name="names" placeholder="alice&#10;bob&#10;carol"
                autofocus></textarea>
      <div class="row" style="margin-top:8px">
        <button class="primary" type="submit">Add to the queue</button>
      </div>
    </form>
  </div>

  <div class="card">
    <form method="post" action="{{ url_for('add_message') }}">
      <label for="mu"><b>Someone sent you a private message</b></label>
      <div class="sub" style="margin:4px 0 8px">For review screenshots, questions,
        anything that arrived in your inbox.</div>
      <input type="text" id="mu" name="username" placeholder="their username">
      <textarea name="body" placeholder="paste what they wrote"
                style="margin-top:8px"></textarea>
      <div class="row" style="margin-top:8px">
        <button type="submit">Add message</button>
      </div>
    </form>
  </div>

  {% if not items %}
    <div class="card empty-state">Nothing waiting. Add someone above.</div>
  {% endif %}

  {% for it in items %}
  <div class="card">
    <div class="row">
      <span class="who">u/{{ it.username }}</span>
      <span class="tag {{ it.tag_class }}">{{ it.tag }}</span>
      {% if it.needs_retry %}<span class="tag help">not sent yet</span>{% endif %}
    </div>
    <div class="hist">{{ it.history }}</div>

    {% if it.said %}<div class="said">{{ it.said }}</div>{% endif %}
    {% for u in it.urls %}
      <div><a href="{{ u }}" target="_blank" rel="noreferrer">{{ u }}</a></div>
    {% endfor %}
    {% if it.gemini %}<div class="hist">Read as: {{ it.gemini }}</div>{% endif %}

    {% if not it.prepared %}
      {% if it.draft %}
        <div class="msg">{{ it.draft }}</div>
        {% if it.ack %}<div class="msg">{{ it.ack }}</div>{% endif %}
        {% if it.next_code %}
          <div class="hist">Will use code <b>{{ it.next_code }}</b></div>
        {% elif it.needs_code %}
          <div class="warnbox">No {{ it.pool }} codes left &mdash; cannot send.</div>
        {% endif %}
        <form method="post" action="{{ url_for('prepare', qid=it.id) }}" class="row">
          <button class="primary" {{ 'disabled' if it.needs_code and not it.next_code }}
                  type="submit">Send &rarr;</button>
          <button formaction="{{ url_for('edit_form', qid=it.id) }}"
                  formmethod="get" type="submit">Edit</button>
          <button formaction="{{ url_for('drop', qid=it.id) }}" class="danger"
                  type="submit">Drop</button>
          <button formaction="{{ url_for('block', qid=it.id) }}" class="danger"
                  type="submit">Block</button>
        </form>
      {% else %}
        <div class="hist">Nothing is drafted &mdash; this one needs a human answer.</div>
        <form method="post" action="{{ url_for('save_draft', qid=it.id) }}">
          <textarea name="body" placeholder="write your reply"></textarea>
          <div class="row" style="margin-top:8px">
            <button class="primary" type="submit">Save reply</button>
            <button formaction="{{ url_for('drop', qid=it.id) }}" class="danger"
                    type="submit">Drop</button>
          </div>
        </form>
      {% endif %}

    {% else %}
      {% for m in it.messages %}
      <div class="step">
        <div class="n">{{ loop.index }}</div>
        <div style="flex:1">
          <b>{{ m.label }}</b>
          <div class="msg" id="m{{ it.id }}-{{ loop.index0 }}">{{ m.body }}</div>
          <div class="row">
            <button onclick="copyIt('m{{ it.id }}-{{ loop.index0 }}', this)"
                    type="button">Copy</button>
            {% if m.url %}
              <a href="{{ m.url }}" target="_blank" rel="noreferrer">
                <button class="primary" type="button">Open Reddit &amp; send</button></a>
            {% endif %}
          </div>
        </div>
      </div>
      {% endfor %}

      <form method="post" class="row" style="margin-top:12px">
        <button class="primary" formaction="{{ url_for('confirm', qid=it.id) }}"
                type="submit">Yes, I sent it</button>
        <button formaction="{{ url_for('unconfirm', qid=it.id) }}"
                type="submit">No, I didn't</button>
      </form>
      <div class="hist" style="margin-top:6px">
        {% if it.allocated %}Code {{ it.allocated }} is held for u/{{ it.username }}
        until you answer.{% endif %}
      </div>
    {% endif %}
  </div>
  {% endfor %}
</div>

<script>
function copyIt(id, btn) {
  const text = document.getElementById(id).innerText;
  navigator.clipboard.writeText(text).then(() => {
    const old = btn.textContent; btn.textContent = "Copied";
    setTimeout(() => btn.textContent = old, 1200);
  }).catch(() => { btn.textContent = "Select it and copy"; });
}
</script>
"""

EDIT_PAGE = """
<!doctype html>
<title>Edit reply</title>
<style>
 body{font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;background:#f6f7f9;
      color:#1a1d21;margin:0}
 @media (prefers-color-scheme:dark){body{background:#14161a;color:#e8eaed}}
 .wrap{max-width:720px;margin:0 auto;padding:24px 16px}
 textarea{width:100%;min-height:220px;padding:12px;font:inherit;border-radius:8px;
          border:1px solid #d7dbe0;background:#fff}
 @media (prefers-color-scheme:dark){textarea{background:#1c1f24;color:#e8eaed;
          border-color:#2c3138}}
 button{font:inherit;font-weight:550;padding:9px 16px;border-radius:8px;
        border:1px solid #2f6feb;background:#2f6feb;color:#fff;cursor:pointer}
 .plain{background:transparent;color:inherit;border-color:#d7dbe0}
 .warn{color:#b45309;font-size:14px}
</style>
<div class="wrap">
  <h2>Reply to u/{{ item.username }}</h2>
  {% if code %}<p class="warn">Keep the code <b>{{ code }}</b> in the text, or
     the message goes out without it.</p>{% endif %}
  <form method="post" action="{{ url_for('save_draft', qid=item.id) }}">
    <textarea name="body">{{ item.draft_body }}</textarea>
    <p><button type="submit">Save</button>
       <a href="{{ url_for('index') }}"><button class="plain" type="button">Cancel</button></a></p>
  </form>
</div>
"""


def create_app(app_id: str | None = None) -> Flask:
    flask_app = Flask(__name__)
    ensure_data_dirs()
    backup_db()

    app_ids = discover_app_ids()
    chosen = app_id or (app_ids[0] if app_ids else None)
    if chosen is None:
        raise SystemExit("No apps configured. Run: promoter add-app <id>")
    cfg = load_app_config(chosen)
    secrets = load_secrets()

    def db():
        # One connection per request: SQLite objects are not shareable across
        # the threads Flask serves from.
        conn = connect()
        init_db(conn)
        return conn

    def classifier():
        return build_classifier(secrets, model=cfg.gemini_model)

    def describe(conn, row) -> dict:
        action = row["action"]
        tags = {
            engine.WEEKLY_CODE: ("weekly code", "code"),
            engine.LIFETIME_CODE: ("lifetime code", "code"),
            engine.DUPLICATE_QUERY: ("already has one", "dup"),
        }
        tag, tag_class = tags.get(action, ("needs you", "help"))

        label = store.get_classification(conn, row["trigger_id"])
        gemini = None
        if label:
            gemini = f"{label['intent']} ({label['confidence']:.0%} sure)"
            if action == engine.REVIEW_ONLY and label["intent"] == "question":
                tag, tag_class = "needs your help", "help"

        user = store.get_user(conn, cfg.app_id, row["username"])
        if not user or user["state"] == store.NEW:
            history = "New - no code from you yet."
        elif user["state"] == store.LIFETIME_SENT:
            history = f"Already has the lifetime code ({user['lifetime_code']})."
        elif user["weekly_code"]:
            history = (f"Got weekly code {user['weekly_code']} on "
                       f"{(user['weekly_sent_at'] or '')[:10]}.")
        else:
            history = f"Recorded as {user['state'].replace('_', ' ')}."

        draft = row["draft_body"]
        undrafted = draft.strip().startswith(engine.NO_DRAFT_PREFIX)
        prepared = row["status"] == store.AWAITING_CONFIRM

        # Read-only: never re-run the allocation just to draw the page.
        messages = actions.rendered_messages(conn, cfg, row) if prepared else []

        return {
            "id": row["id"], "username": row["username"], "tag": tag,
            "tag_class": tag_class, "history": history,
            "said": (row["trigger_body"] or "").strip(),
            "urls": extract_urls(row["trigger_body"]),
            "gemini": gemini,
            "draft": None if undrafted else draft,
            "ack": row["ack_body"],
            "pool": row["pool"],
            "needs_code": action in (engine.WEEKLY_CODE, engine.LIFETIME_CODE),
            "next_code": (row["allocated_code"]
                          or (store.peek_next_code(conn, cfg.app_id, row["pool"])
                              if row["pool"] else None)),
            "allocated": row["allocated_code"],
            "prepared": prepared, "messages": messages,
            "needs_retry": row["status"] == store.NEEDS_RETRY,
        }

    @flask_app.get("/")
    def index():
        conn = db()
        try:
            rows = store.pending_items(conn, cfg.app_id)
            items = [describe(conn, r) for r in rows]
            pools = store.pool_counts(conn, cfg.app_id)
            warnings = []
            orphans = [r for r in backfill.codes_missing_from_files(conn, cfg)
                       if not r["used_by"]]
            if orphans:
                warnings.append(
                    f"{len(orphans)} code(s) are in the database but no longer "
                    f"in your CSV files, and would still be handed out. "
                    f"Run: promoter retire-codes --app {cfg.app_id} "
                    f"--missing --apply")
            return render_template_string(
                PAGE, app_name=cfg.name,
                subreddit=(cfg.subreddits[0] if cfg.subreddits else "?"),
                items=items,
                weekly=pools.get("weekly", {}).get("remaining", 0),
                lifetime=pools.get("lifetime", {}).get("remaining", 0),
                weekly_min=cfg.threshold("weekly"),
                life_min=cfg.threshold("lifetime"),
                people=conn.execute(
                    "SELECT COUNT(*) FROM users WHERE app_id = ?",
                    (cfg.app_id,)).fetchone()[0],
                flash=request.args.get("msg"), warnings=warnings)
        finally:
            conn.close()

    @flask_app.post("/add")
    def add():
        conn = db()
        try:
            names, rejected = backfill.split_usernames(
                request.form.get("names", ""))
            added = 0
            for i, name in enumerate(names):
                item = IncomingComment(
                    item_id=f"web_c_{name}_{time.time_ns()}_{i}",
                    post_id="manual", author=name, body="code", permalink="",
                    is_top_level=True, parent_id="manual",
                    created_utc=time.time())
                if engine.process_comment(conn, cfg, item,
                                          secrets.reddit_username or "me",
                                          classifier()):
                    added += 1
            skipped = len(names) - added
            msg = f"Added {added}."
            if skipped:
                msg += f" {skipped} already handled or blocked."
            if rejected:
                msg += (" Ignored (not valid usernames): "
                        + ", ".join(rejected[:5]))
            return redirect(url_for("index", msg=msg))
        finally:
            conn.close()

    @flask_app.post("/add-message")
    def add_message():
        conn = db()
        try:
            names, _rejected = backfill.split_usernames(
                request.form.get("username", ""))
            body = (request.form.get("body") or "").strip()
            if not names or not body:
                return redirect(url_for(
                    "index",
                    msg="Need a valid username and the message text."))
            item = IncomingMessage(
                item_id=f"web_m_{names[0]}_{time.time_ns()}",
                author=names[0], subject="(pasted by hand)", body=body,
                parent_id=None, created_utc=time.time())
            engine.process_message(conn, cfg, item, classifier(),
                                   app_id=cfg.app_id)
            return redirect(url_for("index", msg="Message added."))
        finally:
            conn.close()

    @flask_app.post("/prepare/<int:qid>")
    def prepare(qid):
        conn = db()
        try:
            row = store.get_queue_item(conn, qid)
            try:
                actions.prepare_send(conn, cfg, row)
            except store.OutOfCodes as exc:
                return redirect(url_for("index", msg=str(exc)))
            except actions.ActionError as exc:
                return redirect(url_for("index", msg=str(exc)))
            return redirect(url_for("index"))
        finally:
            conn.close()

    @flask_app.post("/confirm/<int:qid>")
    def confirm(qid):
        conn = db()
        try:
            row = store.get_queue_item(conn, qid)
            actions.record_sent(conn, cfg, row)
            return redirect(url_for("index",
                                    msg=f"Recorded as sent to u/{row['username']}."))
        finally:
            conn.close()

    @flask_app.post("/unconfirm/<int:qid>")
    def unconfirm(qid):
        conn = db()
        try:
            row = store.get_queue_item(conn, qid)
            actions.record_not_sent(conn, row)
            return redirect(url_for(
                "index",
                msg=f"Nothing recorded. The code stays reserved for "
                    f"u/{row['username']}."))
        finally:
            conn.close()

    @flask_app.get("/edit/<int:qid>")
    def edit_form(qid):
        conn = db()
        try:
            row = store.get_queue_item(conn, qid)
            return render_template_string(EDIT_PAGE, item=row,
                                          code=row["allocated_code"]
                                          or row["preview_code"])
        finally:
            conn.close()

    @flask_app.post("/save/<int:qid>")
    def save_draft(qid):
        conn = db()
        try:
            body = (request.form.get("body") or "").strip()
            if body:
                store.update_draft(conn, qid, body)
                conn.commit()
            return redirect(url_for("index", msg="Reply saved."))
        finally:
            conn.close()

    @flask_app.post("/drop/<int:qid>")
    def drop(qid):
        conn = db()
        try:
            row = store.get_queue_item(conn, qid)
            actions.drop_item(conn, qid, row)
            return redirect(url_for("index", msg="Dropped, nothing sent."))
        finally:
            conn.close()

    @flask_app.post("/block/<int:qid>")
    def block(qid):
        conn = db()
        try:
            row = store.get_queue_item(conn, qid)
            actions.block_user(conn, cfg.app_id, row["username"])
            actions.drop_item(conn, qid, row)
            return redirect(url_for("index",
                                    msg=f"u/{row['username']} blocked."))
        finally:
            conn.close()

    return flask_app


def serve(app_id: str | None = None, port: int = 5000,
          open_browser: bool = True) -> None:
    flask_app = create_app(app_id)
    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    # Bound to loopback on purpose: this reads and writes promo codes.
    flask_app.run(host="127.0.0.1", port=port, debug=False)
