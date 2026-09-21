"""The terminal review dashboard: home summary, then one item at a time.

Nothing leaves this machine without a keypress here.
"""

from __future__ import annotations

import sqlite3

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from . import actions, store
from .config import AppConfig
from .engine import DUPLICATE_QUERY, LIFETIME_CODE, REVIEW_ONLY, WEEKLY_CODE
from .senders import SendError
from .sources.base import extract_urls

ACTION_LABELS = {
    WEEKLY_CODE: ("weekly code", "cyan"),
    LIFETIME_CODE: ("lifetime code", "magenta"),
    DUPLICATE_QUERY: ("duplicate query", "yellow"),
    REVIEW_ONLY: ("needs a decision", "white"),
}


def _fmt_state(state: str | None) -> Text:
    colors = {
        store.NEW: "green",
        store.WEEKLY_CODE_SENT: "cyan",
        store.AWAITING_PROOF: "cyan",
        store.DUPLICATE_QUERY_SENT: "yellow",
        store.LIFETIME_SENT: "magenta",
        store.BLOCKED: "red",
    }
    return Text(state or "-", style=colors.get(state, "white"))


def home(console: Console, conn: sqlite3.Connection, configs: dict[str, AppConfig],
         dry_run: bool = False) -> None:
    """Per-app summary: code pools, users by state, pending queue, warnings."""
    if dry_run:
        console.print(Panel("[bold yellow]DRY RUN[/] - nothing will be sent and "
                            "no code will be allocated", border_style="yellow"))

    apps = store.list_apps(conn)
    if not apps:
        console.print("[yellow]No apps registered yet. Run: add-app <app_id>[/]")
        return

    warnings: list[str] = []

    for app in apps:
        app_id = app["app_id"]
        cfg = configs.get(app_id)
        pools = store.pool_counts(conn, app_id)
        states = store.user_counts_by_state(conn, app_id)
        pending = store.pending_count(conn, app_id)

        table = Table(box=None, pad_edge=False, show_header=True, header_style="dim")
        table.add_column("pool")
        table.add_column("remaining", justify="right")
        table.add_column("used", justify="right")
        table.add_column("total", justify="right")

        for pool in ("weekly", "lifetime"):
            counts = pools.get(pool, {"total": 0, "used": 0, "remaining": 0})
            remaining = counts["remaining"]
            threshold = cfg.threshold(pool) if cfg else 0
            if remaining == 0:
                style, note = "bold red", "  EMPTY - sending halted"
                warnings.append(f"{app_id}: {pool} pool is EMPTY")
            elif threshold and remaining < threshold:
                style, note = "bold yellow", f"  LOW (below {threshold})"
                warnings.append(f"{app_id}: only {remaining} {pool} codes left")
            else:
                style, note = "green", ""
            table.add_row(pool, Text(f"{remaining}{note}", style=style),
                          str(counts["used"]), str(counts["total"]))

        state_bits = "  ".join(f"{k}={v}" for k, v in sorted(states.items())) or "no users yet"
        watches = len(store.active_watches(conn, app_id))

        body = Table.grid(padding=(0, 2))
        body.add_row(table)
        body.add_row(Text(f"users: {state_bits}", style="dim"))
        body.add_row(Text(f"watched posts: {watches}", style="dim"))
        body.add_row(Text(f"pending review: {pending}",
                          style="bold" if pending else "dim"))

        console.print(Panel(body, title=f"[bold]{app['name']}[/] ({app_id})",
                            border_style="blue"))

    if warnings:
        console.print(Panel("\n".join(f"- {w}" for w in warnings),
                            title="[bold yellow]Stock warnings", border_style="yellow"))


def _history(conn, app_id: str | None, username: str) -> Text:
    if not app_id:
        return Text("app unknown - assign one with [a] to see history", style="yellow")
    user = store.get_user(conn, app_id, username)
    if not user:
        return Text("no history for this app - new user", style="green")

    parts = [Text("state: "), _fmt_state(user["state"])]
    if user["weekly_code"]:
        parts.append(Text(f"\nweekly:   {user['weekly_code']}  ({user['weekly_sent_at']})"))
    if user["lifetime_code"]:
        parts.append(Text(f"\nlifetime: {user['lifetime_code']}  ({user['lifetime_sent_at']})"))
    if user["notes"]:
        parts.append(Text(f"\nnotes: {user['notes']}", style="dim"))
    out = Text()
    for p in parts:
        out.append_text(p)
    return out


def _render_item(console: Console, conn, item, cfg: AppConfig | None,
                 position: str, exclude: set[str] | None = None) -> None:
    action = item["action"]
    label, colour = ACTION_LABELS.get(action, (action, "white"))
    app_id = item["app_id"]

    header = Text()
    header.append(f"{position}  ", style="dim")
    header.append(f"[{app_id or 'APP UNKNOWN'}] ", style="bold blue" if app_id else "bold red")
    header.append(f"u/{item['username']}  ")
    header.append(f"{label}", style=f"bold {colour}")
    if item["status"] == store.NEEDS_RETRY:
        header.append("   NEEDS RETRY", style="bold red")
    console.print(header)

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", justify="right", width=12)
    grid.add_column(overflow="fold")

    grid.add_row("history", _history(conn, app_id, item["username"]))

    trigger = item["trigger_body"] or ""
    grid.add_row(f"their {item['trigger_type']}", Text(trigger.strip() or "(empty)"))
    if item["trigger_url"]:
        grid.add_row("link", Text(item["trigger_url"], style="blue underline"))

    urls = extract_urls(trigger)
    if urls:
        grid.add_row("URLs in it", Text("\n".join(urls), style="blue underline"))

    row = store.get_classification(conn, item["trigger_id"])
    if row:
        conf_style = "green" if row["confidence"] >= 0.7 else "yellow"
        grid.add_row("Gemini", Text.assemble(
            (row["intent"], f"bold {conf_style}"),
            (f"  confidence {row['confidence']:.2f}", conf_style),
            (f"\n{row['reason']}", "dim"),
            (f"\nmodel: {row['model']}", "dim"),
        ))

    if item["error"]:
        grid.add_row("last error", Text(item["error"], style="red"))

    console.print(grid)

    if item["subject"]:
        console.print(Text(f"  subject: {item['subject']}", style="dim"))

    draft_style = "white" if action != REVIEW_ONLY else "dim italic"
    console.print(Panel(Text(item["draft_body"], style=draft_style),
                        title="[bold]draft reply (private message)"
                              if action != REVIEW_ONLY else "[bold]no draft",
                        border_style=colour))

    if action in (WEEKLY_CODE, LIFETIME_CODE) and app_id:
        pool = item["pool"]
        reserved = item["allocated_code"]
        if reserved:
            console.print(Text(f"  code {reserved} is already reserved for this user "
                               f"and will be reused on retry", style="yellow"))
        else:
            nxt = store.peek_next_code(conn, app_id, pool, exclude=exclude)
            remaining = store.pool_counts(conn, app_id).get(pool, {}).get("remaining", 0)
            if nxt:
                console.print(Text(f"  will allocate: {nxt}   ({remaining} {pool} "
                                   f"codes left)", style="dim"))
            else:
                console.print(Text(f"  no {pool} codes left - cannot send",
                                   style="bold red"))


def _edit_body(console: Console, current: str) -> str | None:
    console.print("[dim]Enter the replacement text. Finish with a line containing "
                  "only '.' - or '.cancel' to abandon the edit.[/]")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == ".":
            break
        if line.strip() == ".cancel":
            return None
        lines.append(line)
    text = "\n".join(lines).strip()
    return text or None


def review(console: Console, conn: sqlite3.Connection, configs: dict[str, AppConfig],
           sender, *, app_filter: str | None = None, my_username: str = "",
           dry_run: bool = False) -> None:
    """Walk the pending queue one item at a time."""
    home(console, conn, configs, dry_run=dry_run)

    items = store.pending_items(conn, app_filter)
    if not items:
        console.print("\n[green]Nothing pending. All caught up.[/]")
        return

    console.print(f"\n[bold]{len(items)} item(s) to review[/]"
                  + (f" for {app_filter}" if app_filter else "") + "\n")

    ledger = actions.DryRunLedger()
    index = 0
    while index < len(items):
        item = store.get_queue_item(conn, items[index]["id"])
        if item is None or item["status"] not in (store.PENDING, store.NEEDS_RETRY):
            index += 1
            continue

        cfg = configs.get(item["app_id"]) if item["app_id"] else None
        console.rule(style="dim")
        spent = ledger.consumed.get((item["app_id"], item["pool"]), set())
        _render_item(console, conn, item, cfg, f"{index + 1}/{len(items)}",
                     exclude=spent if dry_run else None)

        choices = "s=send  e=edit+send  k=skip  d=drop  b=block  a=assign app  q=quit"
        console.print(f"\n[dim]{choices}[/]")
        key = Prompt.ask("action", choices=["s", "e", "k", "d", "b", "a", "q"],
                         default="k", show_choices=False).strip().lower()

        if key == "q":
            console.print("[dim]Leaving the rest pending.[/]")
            return

        if key == "k":
            actions.skip_item(conn, item["id"])
            console.print("[dim]Skipped - it will come back next time.[/]")
            index += 1
            continue

        if key == "d":
            actions.drop_item(conn, item["id"], item)
            console.print("[dim]Dropped - marked handled, nothing sent.[/]")
            index += 1
            continue

        if key == "b":
            if not item["app_id"]:
                console.print("[red]Assign an app first with [a].[/]")
                continue
            actions.block_user(conn, item["app_id"], item["username"])
            actions.drop_item(conn, item["id"], item)
            console.print(f"[red]u/{item['username']} blocked for "
                          f"{item['app_id']}.[/]")
            index += 1
            continue

        if key == "a":
            available = [a["app_id"] for a in store.list_apps(conn)]
            if not available:
                console.print("[red]No apps registered.[/]")
                continue
            chosen = Prompt.ask("assign to app", choices=available,
                                default=available[0])
            store.reassign_app(conn, item["id"], chosen)
            conn.commit()
            console.print(f"[green]Assigned to {chosen}.[/]")
            continue   # re-render with the app's history and config

        body_override = None
        if key == "e":
            new_body = _edit_body(console, item["draft_body"])
            if new_body is None:
                console.print("[dim]Edit cancelled.[/]")
                continue
            store.update_draft(conn, item["id"], new_body)
            conn.commit()
            item = store.get_queue_item(conn, item["id"])
            body_override = new_body
            console.print(Panel(Text(new_body), title="[bold]edited draft",
                                border_style="green"))
            if Prompt.ask("send this?", choices=["y", "n"], default="n") != "y":
                console.print("[dim]Left pending.[/]")
                index += 1
                continue

        # --- send ---
        if not item["app_id"]:
            console.print("[red]This item has no app. Assign one with [a] first.[/]")
            continue
        cfg = configs.get(item["app_id"])
        if cfg is None:
            console.print(f"[red]No config loaded for app '{item['app_id']}'.[/]")
            continue

        try:
            result = actions.send_item(conn, cfg, item, sender,
                                       body_override=body_override,
                                       my_username=my_username,
                                       dry_run=dry_run, ledger=ledger)
        except store.OutOfCodes as exc:
            console.print(f"[bold red]{exc}[/] - sending halted for this pool. "
                          "Import more codes, then run review again.")
            return
        except actions.ActionError as exc:
            console.print(f"[red]{exc}[/]")
            continue
        except SendError as exc:
            console.print(f"[bold red]Send failed:[/] {exc}")
            console.print("[yellow]Marked needs_retry. Any allocated code stays "
                          "reserved for this user, so retrying will not burn a "
                          "second one.[/]")
            index += 1
            continue

        verb = "Would send" if dry_run else "Sent"
        detail = f" (code {result['code']})" if result["code"] else ""
        console.print(f"[bold green]{verb}[/] to u/{item['username']} via "
                      f"{result['channel']}{detail}")
        if dry_run:
            console.print("[dim]  dry run: nothing allocated, item still "
                          "pending[/]")
        if result.get("note"):
            console.print(f"[yellow]{result['note']}[/]")
        index += 1

    console.print("\n[green]Queue reviewed.[/]")
