"""Command line entry point."""

from __future__ import annotations

import argparse
import re
import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import actions, dashboard, dmcheck, engine, store
from .classify import OfflineClassifier, build_classifier
from .config import (ConfigError, DB_PATH, discover_app_ids, load_app_config,
                     load_secrets)
from .db import backup_db, connect, ensure_data_dirs, init_db
from .senders import RecordingSender
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

    if args.dry_run or args.offline:
        why = "--dry-run" if args.dry_run else "--offline"
        sender = RecordingSender(label=why)
        console.print(f"[yellow]{why}: nothing will actually be sent to Reddit[/]")
        if args.offline and not args.dry_run:
            console.print("[yellow]note: --offline still updates local state; "
                          "use --dry-run to change nothing at all[/]")
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

    if isinstance(sender, RecordingSender) and sender.sent:
        console.print(f"\n[dim]{len(sender.sent)} message(s) would have gone "
                      f"out in a live run.[/]")
    return 0


def cmd_stats(args) -> int:
    conn = _open_db(backup=False)
    configs = _load_configs([args.app] if args.app else None)
    dashboard.home(console, conn, configs)
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
    p.add_argument("--offline", action="store_true",
                   help="walk the queue without contacting Reddit, but still "
                        "update local state")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("check-dm",
                       help="report what the API can do with direct messages")
    p.set_defaults(func=cmd_check_dm)

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
