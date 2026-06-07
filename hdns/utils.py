"""Filter helpers, IP validation, confirmation prompts, audit logging."""

from __future__ import annotations

import fnmatch
import ipaddress
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, TextIO

import click
from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table

from hdns.client import RRset, Zone


console = Console(stderr=False)
err_console = Console(stderr=True)


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


class AuditLogger:
    """Append-only JSON-lines audit log of mutation attempts.

    Use as a context manager or call close() when done. record() is a no-op
    when path is None, so callers don't need to branch.
    """

    def __init__(self, path: Path | None):
        self.path = path
        self._fh: TextIO | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("a", encoding="utf-8")

    def __enter__(self) -> "AuditLogger":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def record(
        self,
        op: str,
        *,
        zone: str,
        record_id: str = "",
        type: str = "",
        name: str = "",
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        result: str = "ok",
        error: str | None = None,
    ) -> None:
        if self._fh is None:
            return
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "op": op,
            "zone": zone,
            "record_id": record_id,
            "type": type,
            "name": name,
            "before": before or {},
            "after": after or {},
            "result": result,
        }
        if error:
            entry["error"] = error
        self._fh.write(json.dumps(entry) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def filter_zones(
    zones: Iterable[Zone],
    *,
    name_glob: str | None = None,
    ttl: int | None = None,
    ttl_min: int | None = None,
    ttl_max: int | None = None,
) -> list[Zone]:
    out = []
    for z in zones:
        if name_glob and not fnmatch.fnmatchcase(z.name, name_glob):
            continue
        if ttl is not None and z.ttl != ttl:
            continue
        if ttl_min is not None and z.ttl < ttl_min:
            continue
        if ttl_max is not None and z.ttl > ttl_max:
            continue
        out.append(z)
    return out


def filter_rrsets(
    rrsets: Iterable[RRset],
    *,
    type: str | Iterable[str] | None = None,
    name_glob: str | None = None,
    value_glob: str | None = None,
    ttl_state: str | None = None,
) -> list[RRset]:
    """Filter rrsets. `type` accepts a single type, a set/list of types, or None.
    `value_glob` matches if ANY value in the rrset matches.
    `ttl_state` is "explicit", "inherited", or None — to filter on whether the
    rrset overrides the zone default."""
    if type is None:
        types_uc: set[str] | None = None
    elif isinstance(type, str):
        types_uc = {type.upper()}
    else:
        types_uc = {t.upper() for t in type}
    out = []
    for rs in rrsets:
        if types_uc and rs.type.upper() not in types_uc:
            continue
        if name_glob and not fnmatch.fnmatchcase(rs.name, name_glob):
            continue
        if value_glob and not any(fnmatch.fnmatchcase(v, value_glob) for v in rs.values()):
            continue
        if ttl_state == "explicit" and rs.ttl is None:
            continue
        if ttl_state == "inherited" and rs.ttl is not None:
            continue
        out.append(rs)
    return out


def classify_ip(addr: str) -> str:
    """Return 'A' for IPv4, 'AAAA' for IPv6. Raises ValueError if not an IP."""
    parsed = ipaddress.ip_address(addr)
    return "AAAA" if isinstance(parsed, ipaddress.IPv6Address) else "A"


@dataclass
class Change:
    """A single proposed mutation, for preview display."""

    target: str  # human label, e.g. "zone example.com" or "A foo.example.com"
    field: str  # "ttl" or "value"
    old: str
    new: str


def render_changes(changes: list[Change], title: str) -> None:
    table = Table(title=title, show_lines=False)
    table.add_column("Target", overflow="fold")
    table.add_column("Field")
    table.add_column("Before", style="red")
    table.add_column("After", style="green")
    for c in changes:
        table.add_row(c.target, c.field, c.old, c.new)
    console.print(table)


def confirm_or_abort(
    changes: list[Change],
    *,
    title: str,
    dry_run: bool,
    assume_yes: bool,
    extra_warning: str | None = None,
) -> bool:
    """Return True if the caller should proceed with the changes.

    Order of operations:
      1. If no changes: print "nothing to do" and return False.
      2. Render preview table.
      3. If dry_run: return False.
      4. If assume_yes: return True.
      5. Otherwise: prompt and return the user's answer.
    """
    if not changes:
        console.print("[yellow]Nothing to do.[/yellow]")
        return False
    render_changes(changes, title)
    if extra_warning:
        err_console.print(f"[bold yellow]{extra_warning}[/bold yellow]")
    if dry_run:
        console.print(f"[blue]Dry-run: {len(changes)} change(s) would be applied.[/blue]")
        return False
    if assume_yes:
        return True
    # Extra friction for large changes.
    n = len(changes)
    if n >= 25:
        err_console.print(
            f"[bold red]You are about to modify {n} records. "
            "Type the number to confirm.[/bold red]"
        )
        try:
            typed = click.prompt("Number of changes to confirm", type=int, default=0)
        except click.Abort:
            return False
        if typed != n:
            err_console.print("[red]Mismatch — aborting.[/red]")
            return False
        return True
    return Confirm.ask(f"Apply {n} change(s)?", default=False)


def die(message: str, exit_code: int = 1) -> None:
    err_console.print(f"[bold red]error:[/bold red] {message}")
    sys.exit(exit_code)
