# Reddit Promo Code Assistant

A human-in-the-loop tool for running promo code giveaways for my mobile apps on
Reddit. It watches my posts and inbox, drafts replies with codes, and sends them
through the Reddit API **only after I approve each one** in a review dashboard.

Deterministic Python + SQLite makes every decision about codes, users, and
state. Gemini is used for one thing only: putting a label on a private message.
Nothing is ever sent without a keypress.

## How the flow works

1. I create the Reddit post manually, with an image. The tool never posts.
2. `watch` the post URL.
3. Someone leaves a **top-level comment** asking for a code.
4. `poll` picks it up and drafts a **private message** carrying a weekly code,
   plus a short public "sent you a DM" reply that never contains a code.
5. `review` shows me the draft. I approve, and the tool sends it.
6. They reply **in that same PM thread** with a screenshot of their review.
7. `poll` picks the reply up, Gemini labels it `proof_submission`, and a
   lifetime-code draft appears in the queue with every URL from their message
   listed so I can open and check the screenshots myself.
8. I approve, and the lifetime code goes out as a reply in the same thread.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
cp .env.example .env            # then fill it in
```

### Registering the Reddit script app

1. Go to <https://www.reddit.com/prefs/apps> while logged in as the account
   that will send the messages.
2. "create another app..." → choose **script**.
3. Name it anything; set the redirect uri to `http://localhost:8080` (script
   apps never use it).
4. The string under the app name is `REDDIT_CLIENT_ID`; the `secret` field is
   `REDDIT_CLIENT_SECRET`.
5. Put those, plus your Reddit username and password, in `.env`.
6. `REDDIT_USER_AGENT` must be descriptive and identify you, e.g.
   `windows:reddit-promoter:0.1.0 (by /u/yourname)`. Reddit rate-limits
   generic user agents hard.

Two-factor auth on the account complicates the password flow, so prefer an
account without it.

### Gemini key

`GEMINI_API_KEY` from <https://aistudio.google.com/apikey>. Without a key the
tool falls back to a crude offline keyword classifier, which is fine for
rehearsals and useless for real triage.

## Commands

```bash
python promoter.py add-app sleepbound              # register from apps/<id>/app.yaml
python promoter.py import-codes --app sleepbound   # idempotent CSV import
python promoter.py watch <post_url> --app sleepbound
python promoter.py unwatch <post_url>
python promoter.py poll                            # build the review queue
python promoter.py poll --fake                     # scripted items, no Reddit
python promoter.py poll --keep-unread              # don't mark the inbox read
python promoter.py review [--app sleepbound]       # the dashboard
python promoter.py review --offline                # review without contacting Reddit
python promoter.py check-dm                        # what the API can do with DMs
python promoter.py post-template --app sleepbound  # the required post format
python promoter.py stats [--app sleepbound]
python promoter.py reset-demo                      # clear local db (keeps a backup)
```

`--dry-run` goes before the subcommand and runs the full pipeline while sending
nothing and allocating nothing:

```bash
python promoter.py --dry-run review
```

`--dry-run` and `--offline` differ: `--offline` walks the queue and really does
update local state (codes get allocated, users move state), it just never
contacts Reddit. `--dry-run` changes nothing at all.

### Dashboard keys

| key | action |
|---|---|
| `s` | send |
| `e` | edit the draft, then send |
| `r` | reword the public reply (pick another wording) |
| `k` | skip for now (stays pending, comes back next run) |
| `d` | drop (mark handled, send nothing, release any reserved code) |
| `b` | block this user for this app |
| `a` | assign an app to an unattributed message |
| `q` | quit, leaving the rest pending |

## Trying it without touching Reddit

The fake source exercises every branch the dashboard handles: a normal request,
a repeat asker, a nested reply, a proof submission with screenshot links, an
off-topic message, a deleted account, and a prompt-injection attempt.

```bash
python promoter.py reset-demo
python promoter.py add-app sleepbound
python promoter.py import-codes --app sleepbound
python promoter.py poll --fake --app sleepbound
python promoter.py --dry-run review     # rehearse: writes nothing
python promoter.py review               # same flow, really updates state
```

## First live run

Do this before pointing it at a real post.

1. `python promoter.py check-dm` — confirms the credentials work and reports
   what the API can see in your inbox.
2. Make a throwaway post in r/test, and comment on it from a second account.
3. `python promoter.py watch <that post url> --app sleepbound`
4. `python promoter.py poll`
5. `python promoter.py --dry-run review` — read the drafts, confirm the right
   codes and the right recipients, send nothing.
6. When it looks right, `python promoter.py review` and approve one item.
   Check the PM and the public reply actually arrived.
7. Reply to that PM from the second account, then `poll` again and confirm the
   reply shows up as a queue item. **This is the step that validates the whole
   proof loop** — see the DM caveat below.

## Posting

r/droidappshowcase requires an exact post format. It lives in
`apps/sleepbound/app.yaml` under `post_template`, so the wording stays in one
place:

```bash
python promoter.py post-template --app sleepbound          # formatted
python promoter.py post-template --app sleepbound --raw    # for copy-paste
```

The tool never posts. Create the post yourself, with the image, then `watch`
its URL.

The public "sent you a DM" reply rotates at random between the wordings listed
under `templates.public_ack.bodies`, so a thread of them does not read as one
bot repeating a sentence. The wording is chosen **when the draft is made**, not
at send time, so the dashboard shows the exact text that will be posted under
your account; `r` picks a different one. No wording may contain `{code}` —
there is a test enforcing that.

## Adding another app

No code changes needed:

1. `apps/<new_app_id>/app.yaml` — display name, store (`play_store` or
   `app_store`), subreddits, code files with their pool and priority, low-stock
   thresholds, and the message templates.
2. Drop its CSVs in `apps/<new_app_id>/codes/`.
3. `python promoter.py add-app <new_app_id>`
4. `python promoter.py import-codes --app <new_app_id>`

Duplicate checks, code pools, user state, and blocks are all per app.

## Safety properties

These are enforced in code and covered by `tests/test_core.py`:

- **A code is never issued twice.** Allocation claims the row with a
  conditional `UPDATE ... WHERE used_by IS NULL`.
- **A cancelled draft never burns a code.** Drafts render a *preview*;
  allocation happens at send time, inside a transaction.
- **A failed Reddit call does not lose the code.** Allocation commits before
  the network call. On failure the item becomes `needs_retry` with the code
  still reserved for that user, and retrying reuses the same code.
- **An edited draft that no longer contains the allocated code is refused**
  rather than sent.
- **The public acknowledgement is best-effort.** If the PM succeeds but the
  public reply fails, the item is still `sent` — it will not re-PM.
- **`--dry-run` writes nothing at all**: no allocation, no state change, no
  audit entry, no queue update.
- **Empty pool halts sending** for that app.
- **Gemini only labels.** It never chooses a code, changes state, decides a
  duplicate, or writes a reply. Confidence below 0.7, malformed JSON, an API
  error or a timeout all become `unclear` and get surfaced with no draft.
- **The original CSVs are never modified.** After import, SQLite is the
  source of truth.
- **Transient Reddit failures back off and retry** (4 attempts, doubling from
  2s); rate limits are waited out. Real refusals - blocked, banned, deleted,
  locked - surface immediately rather than being retried.
- **The inbox is read in full, not just unread**, so a message you happen to
  open on your phone is not lost. `processed_items` prevents reprocessing.
- The database is backed up at the start of each run; the last 10 are kept.

```bash
python -m unittest discover -s tests
```

## Reddit DM caveat (important)

Reddit announced in March 2025 that it is replacing classic Private Messages
with Reddit Chat plus inbox notifications. Classic PMs still work through the
API (`/api/compose`, and PRAW's inbox), and Reddit Chat has **no public API at
all** — PRAW cannot read it, send to it, or even see that a chat exists.

This tool uses classic PMs. The consequence: if someone contacts you through
the **Chat** button rather than replying to the message thread the tool
started, you will never see it here. That is why the public acknowledgement
says "messages, not chat".

Before relying on the proof loop, verify that a reply to a PM the tool sent
comes back into the API-visible inbox: send yourself a PM from a second
account, reply to it, and check that `poll` sees the reply.

## Layout

```
apps/<app_id>/app.yaml        per-app config and message templates
apps/<app_id>/codes/*.csv     code files (gitignored)
src/reddit_promoter/
  config.py                   .env + app.yaml loading
  db.py                       SQLite schema, transactions, backups
  store.py                    codes, users, queue, watches - all state
  engine.py                   incoming items -> drafted queue entries
  actions.py                  approve -> allocate -> send -> record
  senders.py                  the outbound boundary (recording / real)
  classify.py                 message labelling + result cache
  dashboard.py                the terminal review UI
  cli.py                      commands
  sources/                    fake (scripted) and Reddit item sources
data/                         SQLite db + backups (gitignored)
tests/
```

## Status

- [x] Scaffold, config, DB schema, `add-app`, `import-codes`
- [x] Review dashboard driven by the fake source
- [x] PRAW comment polling and sending
- [x] `check-dm` capability check, PM polling, Gemini classification
- [ ] Verified against a real post (needs credentials)

Sleepbound is configured for r/droidappshowcase as u/Remarkable_Pitch_697.
