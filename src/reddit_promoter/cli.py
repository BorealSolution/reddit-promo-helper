"""Command line entry point."""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
import time

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from . import (actions, backfill, clipboard, dashboard, dmcheck, engine,
               store)
from .classify import OfflineClassifier, build_classifier
from .config import (ConfigError, DB_PATH, discover_app_ids, load_app_config,
                     load_secrets)
from .db import backup_db, connect, ensure_data_dirs, init_db
from .senders import ManualSender, RecordingSender
from .sources.base import IncomingComment, IncomingMessage
from .sources.fake import demo_scenario

console = Console()

POST_ID_RE = re.compile(r"/comments/([a-z0-9]+)", re.IGNORECASE)


def _load_configs(app_ids=None) -> dict:
    configs = {}
    for app_id in (app_ids or discover_app_ids()):
        try:
            configs[app_id] = load_app_config(app_id)
        except ConfigError as exc:
            console.print(f"[red]{app_id}: {exc}[/]")
    return configs


def _open_db(backup: bool = True):
    ensure_data_dirs()
    if backup:
        made = backup_db()
        if made:
            console.print(f"[dim]backed up db -> {made.name}[/]")
    conn = connect()
    init_db(conn)
    return conn


def parse_post_id(url_or_id: str) -> str:
    match = POST_ID_RE.search(url_or_id)
    if match:
        return match.group(1)
    if "/" not in url_or_id:
        return url_or_id
    raise ValueError(f"could not find a post id in '{url_or_id}'")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_add_app(args) -> int:
    try:
        cfg = load_app_config(args.app_id)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/]")
        return 1
    conn = _open_db()
    added = store.register_app(conn, cfg)
    console.print(f"[green]{'Registered' if added else 'Updated'}[/] "
                  f"{cfg.name} ({cfg.app_id}, {cfg.store})")
    console.print(f"[dim]code files: "
                  f"{', '.join(f.path.name for f in cfg.code_files)}[/]")
    console.print("[dim]next: import-codes --app " + cfg.app_id + "[/]")
    return 0


def cmd_import_codes(args) -> int:
    try:
        cfg = load_app_config(args.app)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/]")
        return 1
    conn = _open_db()
    store.register_app(conn, cfg)

    try:
        reports = store.import_codes(conn, cfg, dry_run=args.dry_run)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]{exc}[/]")
        return 1

    table = Table(title=f"Code import - {cfg.name}"
                        + (" (DRY RUN, nothing written)" if args.dry_run else ""))
    for col in ("file", "pool", "read", "imported", "already there",
                "dupes in file", "blank"):
        table.add_column(col, justify="right" if col != "file" else "left")

    for r in reports:
        table.add_row(r.source_file, r.pool, str(r.rows_read), str(r.imported),
                      str(r.already_present), str(r.duplicates_in_file),
                      str(r.blank_skipped))
    console.print(table)

    for r in reports:
        if r.duplicate_codes:
            console.print(f"[yellow]{r.source_file}: skipped duplicate codes "
                          f"{', '.join(r.duplicate_codes[:5])}"
                          f"{' ...' if len(r.duplicate_codes) > 5 else ''}[/]")

    counts = store.pool_counts(conn, cfg.app_id)
    for pool, c in sorted(counts.items()):
        console.print(f"  {pool}: [bold]{c['remaining']}[/] unused of {c['total']}")

    # Deleting a used code from a CSV is a natural way to track spend by
    # hand, but import is additive: the code is still in the database and
    # will still be handed out. Say so rather than letting it happen.
    orphans = backfill.codes_missing_from_files(conn, cfg)
    issuable = [r for r in orphans if not r["used_by"]]
    if orphans:
        console.print(f"\n[yellow]{len(orphans)} code(s) in the database are "
                      f"no longer in the CSV files.[/]")
        if issuable:
            console.print(f"[bold red]{len(issuable)} of those are still "
                          f"marked unused and WILL be handed out.[/]")
            console.print("[dim]If you deleted them because they were already "
                          "given away, retire them:[/]")
            console.print("[dim]  promoter.py retire-codes --app "
                          f"{cfg.app_id} --missing --apply[/]")
    return 0


def cmd_watch(args) -> int:
    try:
        cfg = load_app_config(args.app)
        post_id = parse_post_id(args.url)
    except (ConfigError, ValueError) as exc:
        console.print(f"[red]{exc}[/]")
        return 1

    conn = _open_db()
    store.register_app(conn, cfg)

    subreddit = args.subreddit or (cfg.subreddits[0] if cfg.subreddits else "unknown")
    title = None
    secrets = load_secrets()
    if not secrets.missing_reddit() and not args.no_verify:
        from .sources.reddit_source import build_reddit, resolve_post
        try:
            info = resolve_post(build_reddit(secrets), post_id)
            subreddit, title = info["subreddit"], info["title"]
            listed = [x.lower() for x in cfg.subreddits]
            if listed and subreddit.lower() not in listed:
                console.print(f"[yellow]note: that post is in r/{subreddit}, "
                              f"which is not listed in {cfg.app_id}'s "
                              f"app.yaml[/]")
        except Exception as exc:
            console.print(f"[yellow]could not look the post up ({exc}); "
                          f"recording it anyway[/]")

    store.add_watch(conn, post_id, cfg.app_id, subreddit, args.url, title)
    console.print(f"[green]Watching[/] {post_id} in r/{subreddit} for {cfg.name}")
    if title:
        console.print(f"[dim]{title}[/]")
    return 0


def cmd_unwatch(args) -> int:
    try:
        post_id = parse_post_id(args.url)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        return 1
    conn = _open_db()
    if store.deactivate_watch(conn, post_id):
        console.print(f"[green]Stopped watching[/] {post_id}")
    else:
        console.print(f"[yellow]{post_id} was not being watched[/]")
    return 0


def cmd_poll(args) -> int:
    """Fetch new items and build the queue.

    --fake runs the scripted scenario so the whole flow can be exercised
    without credentials; without it, the PRAW source is used.
    """
    conn = _open_db()
    configs = _load_configs()
    if not configs:
        console.print("[red]No apps configured.[/]")
        return 1

    secrets = load_secrets()
    classifier = OfflineClassifier()

    if args.fake:
        console.print("[yellow]FAKE SOURCE[/] - scripted items, no Reddit calls")
        app_id = args.app or sorted(configs)[0]
        cfg = configs[app_id]
        source = demo_scenario()
        store.add_watch(conn, "fakepost1", app_id, "test",
                        "https://reddit.com/r/test/comments/fakepost1")
        my_username = secrets.reddit_username or "me"

        queued = 0
        for comment in source.new_comments("fakepost1"):
            if engine.process_comment(conn, cfg, comment, my_username, classifier):
                queued += 1
        for message in source.new_messages():
            resolved = engine.attribute_app(conn, message.author or "", list(configs))
            target_cfg = configs.get(resolved) if resolved else None
            if engine.process_message(conn, target_cfg, message, classifier,
                                      app_id=resolved):
                queued += 1

        console.print(f"[green]{queued}[/] item(s) queued. "
                      f"Run [bold]review[/] to go through them.")
        return 0

    # --- live ---
    from .sources.reddit_source import (RedditAuthError, RedditSource,
                                        build_reddit, verify_auth)

    try:
        reddit = build_reddit(secrets)
        me = verify_auth(reddit)
    except RedditAuthError as exc:
        console.print(f"[red]{exc}[/]")
        return 1

    console.print(f"[dim]authenticated as u/{me}[/]")
    source = RedditSource(reddit, my_username=me)
    classifier = build_classifier(secrets)
    console.print(f"[dim]classifier: {classifier.model_name}[/]")

    watches = store.active_watches(conn, args.app)
    if not watches:
        console.print("[yellow]No active watched posts. Use: "
                      "watch <url> --app <id>[/]")

    queued = 0
    for watch in watches:
        cfg = configs.get(watch["app_id"])
        if cfg is None:
            console.print(f"[yellow]skipping {watch['post_id']}: no config "
                          f"for app '{watch['app_id']}'[/]")
            continue
        try:
            comments = source.new_comments(watch["post_id"])
        except Exception as exc:
            console.print(f"[red]could not read {watch['post_id']}: {exc}[/]")
            continue
        fresh = 0
        for comment in comments:
            if engine.process_comment(conn, cfg, comment, me, classifier):
                fresh += 1
        queued += fresh
        console.print(f"[dim]{watch['post_id']}: {len(comments)} comment(s) "
                      f"seen, {fresh} new[/]")

    # Private messages. The app is resolved from who the sender is; when that
    # is ambiguous the item is queued unassigned for the dashboard.
    try:
        messages = source.new_messages()
    except Exception as exc:
        console.print(f"[red]could not read the inbox: {exc}[/]")
        messages = []

    handled: list[str] = []
    for message in messages:
        resolved = engine.attribute_app(conn, message.author or "", list(configs))
        target_cfg = configs.get(resolved) if resolved else None
        if engine.process_message(conn, target_cfg, message, classifier,
                                  app_id=resolved):
            queued += 1
        handled.append(message.item_id)

    if messages:
        console.print(f"[dim]inbox: {len(messages)} unread message(s)[/]")
    if handled and not args.keep_unread:
        try:
            source.mark_read(handled)
        except Exception as exc:
            console.print(f"[yellow]could not mark messages read: {exc}[/]")

    console.print(f"[green]{queued}[/] item(s) queued. "
                  f"Run [bold]review[/] to go through them.")
    return 0


def cmd_review(args) -> int:
    conn = _open_db()
    configs = _load_configs()
    secrets = load_secrets()

    have_reddit = not secrets.missing_reddit()

    if args.dry_run:
        sender = RecordingSender(label="dry-run")
        console.print("[yellow]--dry-run: nothing sent, nothing allocated[/]")
    elif args.offline:
        sender = RecordingSender(label="offline")
        console.print("[yellow]--offline: local state is updated but no "
                      "message is produced. Use manual mode to send one.[/]")
    elif args.manual or not have_reddit:
        # Manual mode is the primary path while API access is pending. It
        # needs no Reddit credentials whatsoever.
        if not args.manual:
            console.print("[yellow]No Reddit credentials in .env - running in "
                          "manual mode.[/]")
        console.print("[bold]Manual mode[/]: each approved message is copied "
                      "to your clipboard to paste into Reddit, then you "
                      "confirm it was sent.")
        console.print(f"[dim]clipboard: {clipboard.describe()}[/]")
        sender = ManualSender(console)
    else:
        from .sources.reddit_source import (RedditAuthError, RedditSender,
                                            build_reddit, verify_auth)
        try:
            reddit = build_reddit(secrets)
            me = verify_auth(reddit)
        except RedditAuthError as exc:
            console.print(f"[red]{exc}[/]")
            console.print("[dim]To review drafts without credentials, use "
                          "--dry-run or --offline.[/]")
            return 1
        console.print(f"[dim]sending as u/{me} - approvals go to Reddit[/]")
        sender = RedditSender(reddit)

    dashboard.review(console, conn, configs, sender,
                     app_filter=args.app,
                     my_username=secrets.reddit_username,
                     dry_run=args.dry_run)

    if isinstance(sender, ManualSender) and sender.sent:
        console.print(f"\n[green]{len(sender.sent)} message(s) sent by hand "
                      f"and recorded.[/]")
    elif isinstance(sender, RecordingSender) and sender.sent:
        console.print(f"\n[dim]{len(sender.sent)} message(s) would have gone "
                      f"out in a live run.[/]")
    return 0


def cmd_stats(args) -> int:
    conn = _open_db(backup=False)
    configs = _load_configs([args.app] if args.app else None)
    dashboard.home(console, conn, configs)
    return 0


def _read_multiline(prompt: str) -> str:
    console.print(f"[dim]{prompt} Finish with a line containing only '.'[/]")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == ".":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def cmd_add(args) -> int:
    """Enter a comment or message by hand, with no Reddit access at all.

    This is the primary path while API access is pending: paste in who said
    what, and the queue is built exactly as a live poll would build it.
    """
    try:
        cfg = load_app_config(args.app)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/]")
        return 1

    conn = _open_db()
    store.register_app(conn, cfg)
    secrets = load_secrets()
    classifier = build_classifier(secrets, model=cfg.gemini_model)
    me = secrets.reddit_username or "me"

    # Bulk: several people who all just asked for a code.
    if args.users:
        names, rejected = backfill.split_usernames(args.users)
        if rejected:
            console.print(f"[yellow]ignored (not valid usernames): "
                          f"{', '.join(rejected[:5])}[/]")
        queued = 0
        for name in names:
            item = IncomingComment(
                item_id=f"manual_c_{name}_{int(time.time() * 1000)}",
                post_id=args.post or "manual",
                author=name,
                body=args.comment or "code",
                permalink=args.url or "",
                is_top_level=True,
                parent_id="manual",
                created_utc=time.time(),
            )
            if engine.process_comment(conn, cfg, item, me, classifier):
                queued += 1
        console.print(f"[green]{queued}[/] of {len(names)} added "
                      f"({len(names) - queued} already known or blocked).")
        console.print("[dim]next: review[/]")
        return 0

    username = args.user
    body = args.comment or args.message
    kind = "message" if args.message else "comment"

    # Interactive when nothing was given on the command line.
    if not username:
        username = Prompt.ask("reddit username").strip()
    username = backfill.normalise_usernames([username])
    if not username:
        console.print("[red]no username given[/]")
        return 1
    username = username[0]

    if not body:
        kind = Prompt.ask("what is it", choices=["comment", "message"],
                          default="comment")
        body = _read_multiline(f"Paste their {kind}.")
    if not body:
        console.print("[red]no text given[/]")
        return 1

    stamp = int(time.time() * 1000)
    if kind == "message":
        item = IncomingMessage(
            item_id=f"manual_m_{username}_{stamp}",
            author=username,
            subject=args.subject or "(pasted by hand)",
            body=body,
            parent_id=None,
            created_utc=time.time(),
        )
        console.print(f"[dim]classifier: {classifier.model_name}[/]")
        qid = engine.process_message(conn, cfg, item, classifier,
                                     app_id=cfg.app_id)
    else:
        item = IncomingComment(
            item_id=f"manual_c_{username}_{stamp}",
            post_id=args.post or "manual",
            author=username,
            body=body,
            permalink=args.url or "",
            is_top_level=not args.nested,
            parent_id="manual",
            created_utc=time.time(),
        )
        qid = engine.process_comment(conn, cfg, item, me, classifier)

    if qid is None:
        console.print("[yellow]Nothing queued - that user is blocked, or the "
                      "item was already handled.[/]")
        return 0

    row = store.get_queue_item(conn, qid)
    console.print(f"[green]Queued #{qid}[/]: {row['action']} for "
                  f"u/{username}")
    console.print("[dim]next: review[/]")
    return 0


def cmd_backfill(args) -> int:
    """Recover who was already given a code, before this tool existed.

    Reads the sent-messages folder. Without this the duplicate check is blind
    to everyone served by hand: they comment again and look brand new.
    """
    try:
        cfg = load_app_config(args.app)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/]")
        return 1

    conn = _open_db()
    store.register_app(conn, cfg)

    # Usernames given by hand need no Reddit access at all.
    raw_names = args.users or ""
    if args.users_file:
        path = pathlib.Path(args.users_file)
        if not path.exists():
            console.print(f"[red]no such file: {path}[/]")
            return 1
        # One per line, or comma-separated, or both. Blank lines and lines
        # starting with # are ignored so the list can be annotated.
        lines = [ln.split("#", 1)[0] for ln in path.read_text(
            encoding="utf-8").splitlines()]
        raw_names = raw_names + " " + " ".join(lines)

    if raw_names.strip():
        names, rejected = backfill.split_usernames(raw_names)
        if rejected:
            console.print(f"[yellow]ignored (not valid usernames): "
                          f"{', '.join(rejected[:8])}[/]")
        if not names:
            console.print("[red]no valid usernames found[/]")
            return 1
        already = [n for n in names
                   if store.get_user(conn, cfg.app_id, n) is not None]
        if not args.apply:
            console.print(f"[yellow]Would mark {len(names)} user(s) as already "
                          f"served:[/]")
            for n in names:
                note = "  (already recorded)" if n in already else ""
                console.print(f"  u/{n}{note}")
            console.print("[dim]re-run with the same command plus --apply to "
                          "write it[/]")
            return 0
        done = backfill.mark_users_served(conn, cfg.app_id, names)
        console.print(f"[green]Marked {done} user(s) as already served.[/] "
                      f"They will now get a duplicate query instead of a "
                      f"second code.")
        return 0

    from .sources.reddit_source import (RedditAuthError, build_reddit,
                                        verify_auth)
    secrets = load_secrets()
    try:
        reddit = build_reddit(secrets)
        me = verify_auth(reddit)
    except RedditAuthError as exc:
        console.print(f"[red]{exc}[/]")
        console.print("[dim]Without credentials you can still record people "
                      "by name: backfill --app <id> --users alice,bob "
                      "--apply[/]")
        return 1

    console.print(f"[dim]reading the sent folder of u/{me}...[/]")
    try:
        report = backfill.scan_sent(reddit, conn, cfg.app_id, limit=args.limit)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/]")
        return 1

    console.print(f"[dim]scanned {report.scanned} sent message(s); "
                  f"{report.skipped_no_code} contained no code[/]")

    if not report.found:
        console.print("[yellow]No codes found in your sent messages.[/]")
        console.print("[dim]If you handed codes out another way, record those "
                      "people with --users alice,bob[/]")
        return 0

    # Codes are 23 characters and must be readable in full - a truncated code
    # cannot be checked against anything.
    table = Table(title=f"Codes already sent - {cfg.name}"
                        + ("" if args.apply else "  (PREVIEW, nothing written)"),
                  box=None, pad_edge=False, header_style="dim")
    table.add_column("user", overflow="fold")
    table.add_column("pool")
    table.add_column("code", no_wrap=True)
    table.add_column("sent")
    table.add_column("note", overflow="fold")

    for f in report.found:
        if not f.in_pool:
            note = "older batch, not in the CSVs"
        elif f.already_used_by and f.already_used_by != f.username:
            note = f"CONFLICT: CSV says u/{f.already_used_by}"
        elif f.already_used_by == f.username:
            note = "already recorded"
        else:
            note = "in the CSVs, will be retired"
        if f.already_known:
            note += f"; user already {f.already_known}"
        table.add_row(f"u/{f.username}", f.pool, f.code, f.sent_at[:10], note)
    console.print(table)

    for conflict in report.conflicts:
        console.print(f"[yellow]conflict: {conflict}[/]")

    in_pool = sum(1 for f in report.found if f.in_pool and not f.already_used_by)
    console.print(f"\n[bold]{len(report.users)}[/] user(s) already served; "
                  f"[bold]{in_pool}[/] of those codes are still marked unused "
                  f"in your CSVs and would be retired.")

    if not args.apply:
        console.print("[dim]re-run with --apply to record it[/]")
        return 0

    backfill.apply_backfill(conn, cfg.app_id, report)
    console.print(f"[green]Recorded {report.applied} user(s); "
                  f"retired {report.codes_marked_used} code(s).[/]")
    counts = store.pool_counts(conn, cfg.app_id)
    for pool, c in sorted(counts.items()):
        console.print(f"  {pool}: {c['remaining']} unused of {c['total']}")
    return 0


def cmd_reward(args) -> int:
    """Draft the lifetime code for someone whose review I have checked."""
    try:
        cfg = load_app_config(args.app)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/]")
        return 1

    conn = _open_db()
    store.register_app(conn, cfg)

    names, rejected = backfill.split_usernames(args.user or "")
    if rejected:
        console.print(f"[yellow]ignored: {', '.join(rejected)}[/]")
    if not names:
        console.print("[red]give a valid username with --user[/]")
        return 1

    for name in names:
        reason = engine.lifetime_block_reason(conn, cfg.app_id, name)
        if reason:
            console.print(f"[yellow]{reason}[/]")
            continue
        qid = engine.queue_lifetime_reward(conn, cfg, name)
        if qid is None:
            console.print(f"[yellow]could not draft one for u/{name}[/]")
            continue
        console.print(f"[green]Lifetime code drafted for u/{name}[/] "
                      f"(queue #{qid})")
    console.print("[dim]next: review, or the browser page[/]")
    return 0


def cmd_web(args) -> int:
    """Open the point-and-click version in a browser."""
    from . import web
    console.print(f"[bold]Opening http://127.0.0.1:{args.port}/[/] in your "
                  f"browser")
    console.print("[dim]Leave this window open while you use it. "
                  "Press Ctrl+C here when you are done.[/]")
    try:
        web.serve(app_id=args.app, port=args.port,
                  open_browser=not args.no_browser)
    except OSError as exc:
        console.print(f"[red]Could not start the server: {exc}[/]")
        console.print(f"[dim]Something may already be using port {args.port}. "
                      f"Try: promoter web --port 5001[/]")
        return 1
    return 0


def cmd_retire_codes(args) -> int:
    """Mark specific codes as already spent so they are never issued.

    Needed because deleting a code from a CSV does not remove it from the
    database - import is additive, so the code stays issuable.
    """
    try:
        cfg = load_app_config(args.app)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/]")
        return 1

    conn = _open_db()
    store.register_app(conn, cfg)

    pairs: list[tuple[str, str | None]] = []

    if args.missing:
        orphans = backfill.codes_missing_from_files(conn, cfg)
        pairs += [(r["code"], None) for r in orphans if not r["used_by"]]
        if not pairs:
            console.print("[green]Nothing to do: every unused code in the "
                          "database is still present in the CSVs.[/]")
            return 0
        console.print(f"[dim]{len(pairs)} code(s) are in the database but no "
                      f"longer in the CSVs[/]")

    if args.codes:
        pairs += backfill.parse_codes_file(args.codes.replace(",", "\n"))
    if args.codes_file:
        path = pathlib.Path(args.codes_file)
        if not path.exists():
            console.print(f"[red]no such file: {path}[/]")
            return 1
        pairs += backfill.parse_codes_file(path.read_text(encoding="utf-8"))

    if not pairs:
        console.print("[red]Nothing given. Use --codes, --codes-file or "
                      "--missing.[/]")
        return 1

    report = backfill.retire_codes(conn, cfg.app_id, pairs, apply=args.apply)

    if report.malformed:
        console.print(f"[yellow]{len(report.malformed)} line(s) were not "
                      f"23-character codes and were skipped:[/] "
                      + ", ".join(report.malformed[:5]))
    if report.not_in_pool:
        console.print(f"[dim]{len(report.not_in_pool)} code(s) are not in this "
                      f"app's pool at all (an older batch) - nothing to "
                      f"retire[/]")
    if report.already_retired:
        console.print(f"[dim]{len(report.already_retired)} code(s) were "
                      f"already marked used[/]")
    for conflict in report.conflicts:
        console.print(f"[yellow]conflict: {conflict}[/]")

    if not report.retired:
        console.print("[green]No codes needed retiring.[/]")
        return 0

    verb = "Retired" if args.apply else "Would retire"
    console.print(f"[bold]{verb} {len(report.retired)} code(s)[/]")
    for code, holder in report.retired[:20]:
        who = "" if holder == backfill.SPENT_UNKNOWN else f"  -> u/{holder}"
        console.print(f"  {code}{who}")
    if len(report.retired) > 20:
        console.print(f"  ... and {len(report.retired) - 20} more")

    if not args.apply:
        console.print("[dim]re-run with --apply to write it[/]")
        return 0

    counts = store.pool_counts(conn, cfg.app_id)
    for pool, c in sorted(counts.items()):
        console.print(f"  {pool}: [bold]{c['remaining']}[/] still available "
                      f"of {c['total']}")
    return 0


def cmd_post_template(args) -> int:
    """Print the post format for an app, ready to paste into Reddit.

    The tool never posts - r/droidappshowcase requires an exact format, so
    this keeps the wording in one place instead of in my notes.
    """
    try:
        cfg = load_app_config(args.app)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/]")
        return 1

    tpl = cfg.post_template
    if not tpl:
        console.print(f"[yellow]{cfg.app_id} has no post_template in "
                      f"app.yaml[/]")
        return 1

    sub = tpl.get("subreddit") or (cfg.subreddits[0] if cfg.subreddits else "?")
    if args.raw:
        print(tpl.get("title", ""))
        print()
        print(tpl.get("body", ""))
        return 0

    console.print(f"[dim]r/{sub}[/]")
    console.print(Panel(tpl.get("title", ""), title="[bold]title",
                        border_style="blue"))
    console.print(Panel(tpl.get("body", "").rstrip(), title="[bold]body",
                        border_style="blue"))
    console.print("[dim]--raw prints it unformatted for copy-paste. "
                  "Remember the image.[/]")
    return 0


def cmd_check_dm(args) -> int:
    """Report what the Reddit API can actually do with direct messages."""
    from .sources.reddit_source import RedditAuthError
    secrets = load_secrets()
    try:
        report = dmcheck.run_check(secrets)
    except RedditAuthError as exc:
        console.print(f"[red]{exc}[/]")
        return 1
    dmcheck.print_report(console, report)
    return 0


def cmd_reset_demo(args) -> int:
    """Wipe the local db so the fake-source walkthrough can be re-run."""
    if DB_PATH.exists():
        backup_db()
        DB_PATH.unlink()
        for suffix in ("-wal", "-shm"):
            extra = DB_PATH.with_name(DB_PATH.name + suffix)
            extra.unlink(missing_ok=True)
        console.print("[green]Local database cleared[/] (a backup was kept).")
    else:
        console.print("[dim]No database to clear.[/]")
    return 0


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="promoter",
        description="Human-in-the-loop Reddit promo code assistant.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="run the full pipeline but send nothing and "
                             "allocate nothing")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add-app", help="register an app from apps/<id>/app.yaml")
    p.add_argument("app_id")
    p.set_defaults(func=cmd_add_app)

    p = sub.add_parser("import-codes", help="idempotent import of an app's CSVs")
    p.add_argument("--app", required=True)
    p.set_defaults(func=cmd_import_codes)

    p = sub.add_parser("watch", help="start monitoring a post")
    p.add_argument("url")
    p.add_argument("--app", required=True)
    p.add_argument("--subreddit")
    p.add_argument("--no-verify", action="store_true",
                   help="do not look the post up on Reddit first")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("unwatch", help="stop monitoring a post")
    p.add_argument("url")
    p.set_defaults(func=cmd_unwatch)

    p = sub.add_parser("poll", help="fetch new comments/messages, build the queue")
    p.add_argument("--app")
    p.add_argument("--fake", action="store_true",
                   help="use the scripted demo source instead of Reddit")
    p.add_argument("--keep-unread", action="store_true",
                   help="do not mark inbox messages read (useful while testing)")
    p.set_defaults(func=cmd_poll)

    p = sub.add_parser("review", help="open the review dashboard")
    p.add_argument("--app")
    p.add_argument("--manual", action="store_true",
                   help="copy each approved message to the clipboard for you "
                        "to paste (the default when there are no Reddit "
                        "credentials)")
    p.add_argument("--offline", action="store_true",
                   help="walk the queue without contacting Reddit, but still "
                        "update local state")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("check-dm",
                       help="report what the API can do with direct messages")
    p.set_defaults(func=cmd_check_dm)

    p = sub.add_parser("add",
                       help="enter a comment or message by hand (needs no "
                            "Reddit access)")
    p.add_argument("--app", required=True)
    p.add_argument("--user", help="the reddit username")
    p.add_argument("--users",
                   help="comma-separated usernames who all asked for a code")
    p.add_argument("--comment", help="the text of their comment")
    p.add_argument("--message", help="the text of their private message")
    p.add_argument("--subject", help="subject of their message")
    p.add_argument("--nested", action="store_true",
                   help="a reply under one of my comments, not a top-level "
                        "request")
    p.add_argument("--url", help="permalink to their comment")
    p.add_argument("--post", help="post id the comment is on")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("backfill",
                       help="record people you already gave codes to by hand")
    p.add_argument("--app", required=True)
    p.add_argument("--limit", type=int, default=500,
                   help="how many sent messages to scan (default 500)")
    p.add_argument("--users",
                   help="comma- or space-separated usernames to mark as "
                        "already served")
    p.add_argument("--users-file",
                   help="a text file of usernames, one per line (# comments "
                        "allowed)")
    p.add_argument("--apply", action="store_true",
                   help="actually write it (default is a preview)")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("reward",
                       help="send the lifetime code to someone whose review "
                            "you have checked")
    p.add_argument("--app", required=True)
    p.add_argument("--user", required=True)
    p.set_defaults(func=cmd_reward)

    p = sub.add_parser("web", help="open the point-and-click version in a browser")
    p.add_argument("--app")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--no-browser", action="store_true",
                   help="do not open a browser window automatically")
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("retire-codes",
                       help="mark specific codes as already spent")
    p.add_argument("--app", required=True)
    p.add_argument("--codes", help="comma-separated codes")
    p.add_argument("--codes-file",
                   help="a file of codes, one per line, optionally followed "
                        "by the username who got it")
    p.add_argument("--missing", action="store_true",
                   help="retire every unused code that is in the database "
                        "but no longer in the CSV files")
    p.add_argument("--apply", action="store_true",
                   help="actually write it (default is a preview)")
    p.set_defaults(func=cmd_retire_codes)

    p = sub.add_parser("post-template",
                       help="print an app's required post format")
    p.add_argument("--app", required=True)
    p.add_argument("--raw", action="store_true",
                   help="plain text, no formatting")
    p.set_defaults(func=cmd_post_template)

    p = sub.add_parser("stats", help="per-app summary")
    p.add_argument("--app")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("reset-demo", help="clear the local db (keeps a backup)")
    p.set_defaults(func=cmd_reset_demo)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "dry_run"):
        args.dry_run = False
    try:
        return args.func(args)
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted. Nothing further was sent.[/]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
