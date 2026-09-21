# Reddit Promo Code Assistant

A human-in-the-loop tool for running promo code giveaways for Android apps on
Reddit. It tracks who asked for a code, who already got one, and who has sent
proof of a review — then drafts the reply with the right code filled in and
waits for approval before anything goes out.

Deterministic Python + SQLite makes every decision about codes, users and
state. An LLM is used for exactly one thing: putting a label on an incoming
message. It never chooses a code, changes a state, or writes a reply.

Currently configured for **SleepBound** in r/droidappshowcase.

## Status

| | |
|---|---|
| **Manual mode** | **Working. This is the path to use.** No Reddit API access needed. |
| API mode | Written and tested, but dormant — waiting on Reddit API approval. |

Reddit API approval can take weeks or never arrive, so manual mode is the
primary path, not a placeholder. It does everything except the final click:
the tool tracks state, picks the code, drafts the message and copies it to
your clipboard; you paste it into Reddit and confirm. Nothing is recorded as
sent unless you say it was.

Switching to API mode later means filling in `.env` and changing nothing else.

## What it does for you

- **Stops double-issuing codes.** The thing that is genuinely hard to do by
  hand across hundreds of comments. A repeat asker is spotted automatically
  and gets a "did you need a second one?" question instead of another code.
- **Allocates in order** from the CSVs, oldest batch first, and never hands
  out the same code twice.
- **Tracks the review→lifetime flow**, so you know who owes proof and who has
  already been upgraded.
- **Flags people who need help** separately from routine code requests.
- **Keeps an audit log** of every allocation, send and state change.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate           # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements.txt

python promoter.py add-app sleepbound
python promoter.py import-codes --app sleepbound
```

That is the whole setup for manual mode. No `.env` is required.

Optionally, `GEMINI_API_KEY` in `.env` (from
<https://aistudio.google.com/apikey>) improves how incoming private messages
are labelled. Without it, a crude offline keyword classifier is used — fine to
start with, and it never blocks anything.

## Daily use (the browser version)

```powershell
.\promoter.cmd web
```

1. **Paste usernames** into the top box and press *Add to the queue*. Commas,
   spaces or new lines all work; `u/name` is fine. Anything that is not a
   valid username is rejected and listed rather than becoming a junk entry.
2. Each person appears as a card with their history and the drafted message.
   Someone who already has a code is labelled and gets a question instead.
3. Press **Send**. The code is allocated and the message shown with a
   *Copy* button and an *Open Reddit & send* link that has the message
   already filled in.
4. Send it in the tab that opens, come back, and press **Yes, I sent it**
   (or **No, I didn't**).

Nothing is allocated until step 3, and nothing counts as sent until step 4.
If you close the browser midway, the person is still in the queue with their
code held for them.

**When someone sends you a review screenshot:** look at it, and if you are
happy, put their username in the third box, *They left a review*. The
lifetime code is drafted straight away - no need to paste their message or
hope it gets recognised. From a terminal that is
`.\promoter.cmd reward --app sleepbound --user NAME`.

Someone who already has a lifetime code is refused, with the code they were
given.

Private messages you receive go in the second box. Review screenshots are
recognised and a lifetime-code reply is drafted, with the links pulled out so
you can check them first.

## Daily use (the terminal version)

**1. Post.** r/droidappshowcase requires an exact format, kept in `app.yaml`:

```bash
python promoter.py post-template --app sleepbound --raw
```

Post it yourself with the image. The tool never posts.

**2. Record who asked.** As comments come in:

```bash
# several people who all commented "code"
python promoter.py add --app sleepbound --users alice,bob,carol

# one person, with their actual words
python promoter.py add --app sleepbound --user alice --comment "can I get a code?"

# a private message they sent you
python promoter.py add --app sleepbound --user alice --message "reviewed it! https://imgur.com/a/x"

# or run it with no flags and it will prompt
python promoter.py add --app sleepbound
```

**3. Review and send.**

```bash
python promoter.py review
```

For each item you see their history, what they said, and the drafted reply
with a real code in it. Press `s` and the tool copies the message to your
clipboard, shows a prefilled Reddit compose link, and asks whether you sent
it. Answer `y` and the code is marked used and their state moves; answer `n`
and nothing is recorded — the code stays reserved for that person so retrying
reuses it rather than burning a second one.

| key | action |
|---|---|
| `s` | send (copies to clipboard, then confirms) |
| `e` | write or edit the reply, then send |
| `r` | reword the public "sent you a DM" reply |
| `k` | skip for now — comes back next time |
| `d` | drop — handled, nothing sent, releases any reserved code |
| `b` | block this user for this app |
| `a` | assign an app to an unattributed message |
| `q` | quit, leaving the rest pending |

**4. Check stock.**

```bash
python promoter.py stats
```

### Before your first run: record past giveaways

The duplicate check only knows what this tool recorded. Anyone you gave a code
to by hand looks brand new and would be served twice:

Open <https://www.reddit.com/message/sent/>, note who you already sent a code
to, and put the names in a file (one per line; `#` comments allowed):

```bash
python promoter.py backfill --app sleepbound --users-file past-recipients.txt
python promoter.py backfill --app sleepbound --users-file past-recipients.txt --apply
```

Or inline for a few: `--users alice,bob,carol`. `u/alice` and `/u/alice` work
too. The first command previews and writes nothing; only `--apply` records.
Re-running is harmless, so add names as you remember them.

Once Reddit API access is approved, `backfill --app sleepbound` with no
`--users` reads your sent folder and works all this out by itself.

Do this **before** the first review session: anyone missing gets a second
code, and that cannot be undone afterwards.

### If you have already spent some codes

Deleting a used code from a CSV does **not** retire it. Import is additive:
the code is already in the database and will still be handed out. `retire-codes`
is the only thing that stops that.

```bash
# codes you have already given away, one per line
# (optionally followed by who got it, to record them too)
python promoter.py retire-codes --app sleepbound --codes-file spent-codes.txt
python promoter.py retire-codes --app sleepbound --codes-file spent-codes.txt --apply

# or, if you track spend by deleting rows from the CSV:
python promoter.py retire-codes --app sleepbound --missing --apply
```

`--missing` retires every unused code that is in the database but no longer in
the CSVs. `import-codes` warns whenever that gap appears.

Marking a *user* as served and retiring a *code* are different things, and
both matter: the first stops them getting a second code, the second stops a
dead code going to somebody else.

## Clipboard support

| OS | Backend | Notes |
|---|---|---|
| Windows | `clip.exe` | Built in. Verified here, including non-ASCII. |
| macOS | `pbcopy` | Built in. |
| Linux | `wl-copy`, `xclip` or `xsel` | Install one, e.g. `apt install xclip`. |
| any | `tkinter` | Automatic fallback if the above fails. |

If no clipboard is available the message is printed in full so you can copy it
by hand — the workflow never blocks on it. `review` prints which backend it is
using at startup.

## Try it without sending anything

```bash
python promoter.py reset-demo
python promoter.py add-app sleepbound
python promoter.py import-codes --app sleepbound
python promoter.py poll --fake --app sleepbound   # scripted people
python promoter.py --dry-run review               # changes nothing at all
```

`--dry-run` runs the whole pipeline and writes nothing: no allocation, no
state change, no audit entry.

## Safety properties

Enforced in code, covered by 135 tests (`python -m unittest discover -s tests`):

- **A code is never issued twice** — allocation claims the row with a
  conditional `UPDATE ... WHERE used_by IS NULL`.
- **Nothing is allocated at draft time.** Drafts show a *preview*; the real
  allocation happens at the moment you approve. Skipping or dropping a draft
  burns nothing.
- **A send that did not happen is never recorded as sent.** Answering "no",
  a crash, or input closing mid-prompt all mark the item `needs_retry` with
  the code still reserved for that person, so the retry reuses it.
- **No pending item ever holds an allocated code** — an invariant a real bug
  once violated; there is a regression test for it.
- **An edited draft that no longer contains the allocated code is refused.**
- **The public reply can never contain a code** — enforced by a test, since
  that reply is the only thing posted publicly.
- **Empty pool halts sending** for that app.
- **The LLM only labels.** Low confidence, malformed output, an API error or
  a timeout all become "unclear" and are surfaced with no draft. The prompt
  states the message is untrusted data, and a message trying to instruct the
  classifier still cannot cause a code to be drafted.
- **The original CSVs are never modified.** After import, SQLite is the source
  of truth. The database is backed up at the start of every run.

## Adding another app

No code changes: create `apps/<id>/app.yaml`, drop its CSVs in
`apps/<id>/codes/`, then `add-app` and `import-codes`. Codes, duplicate
checks, user state and blocks are all per app.

## API mode (pending Reddit approval)

Once a Reddit script app is approved, fill in `.env` (see `.env.example`) and
`review` switches from manual to sending directly; `poll` then reads comments
and messages instead of `add`. `python promoter.py check-dm` reports what the
API can actually see.

One caveat that will matter: Reddit is replacing classic private messages with
**Reddit Chat**, which has no public API. Classic PMs work; anything sent via
the Chat button is invisible to the tool. That is also why the public reply
says "messages, not chat".

## Layout

```
apps/<app_id>/app.yaml      per-app config, post format, message templates
apps/<app_id>/codes/*.csv   code files (gitignored)
src/reddit_promoter/
  config.py                 app.yaml + .env loading
  db.py                     SQLite schema, transactions, backups
  store.py                  codes, users, queue - all state
  engine.py                 incoming items -> drafted queue entries
  actions.py                approve -> allocate -> send -> record
  senders.py                manual (clipboard) and API senders
  clipboard.py              cross-platform clipboard
  classify.py               message labelling + result cache
  dashboard.py              the terminal review UI
  backfill.py               recover codes given out by hand
  cli.py                    commands
  sources/                  fake, and Reddit (dormant)
data/                       SQLite db + backups (gitignored)
tests/
```

## Commands

```
add-app <id>              register an app from apps/<id>/app.yaml
import-codes --app <id>   idempotent CSV import
add --app <id>            enter a comment or message by hand
review [--app <id>]       the approval dashboard
stats [--app <id>]        code stock and user states
backfill --app <id>       record codes given out by hand
post-template --app <id>  print the required post format
reset-demo                clear the local database (keeps a backup)

poll [--fake]             fetch from Reddit (API mode) or run the demo source
watch <url> --app <id>    monitor a post (API mode)
unwatch <url>
check-dm                  report what the API can see (API mode)

--dry-run                 global: run everything, change nothing
```
