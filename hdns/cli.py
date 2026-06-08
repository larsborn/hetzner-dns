"""hdns CLI."""

from __future__ import annotations

import csv
import json
import os
import re
import sys
from pathlib import Path

import click
from rich.table import Table

from hdns import __version__
from hdns.client import HetznerDnsClient, HetznerDnsError, RRset, Zone
from hdns.utils import (
    AuditLogger,
    Change,
    classify_ip,
    confirm_or_abort,
    console,
    die,
    err_console,
    filter_rrsets,
    filter_zones,
    setup_logging,
)


TOKEN_ENV = "HCLOUD_TOKEN"


def _client() -> HetznerDnsClient:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        die(f"{TOKEN_ENV} is not set. Export your Hetzner Cloud API token.")
    return HetznerDnsClient(token)


def _fetch_zones(
    client: HetznerDnsClient,
    name_glob: str | None,
    label_selector: str | None = None,
    *,
    ttl: int | None = None,
    ttl_min: int | None = None,
    ttl_max: int | None = None,
) -> list[Zone]:
    _validate_zone_ttl_filters(ttl, ttl_min, ttl_max)
    try:
        zones = client.list_zones(label_selector=label_selector)
    except HetznerDnsError as e:
        die(f"failed to list zones: {e} ({e.body or ''})")
    return filter_zones(
        zones, name_glob=name_glob, ttl=ttl, ttl_min=ttl_min, ttl_max=ttl_max,
    )


def _fetch_all_rrsets(
    client: HetznerDnsClient,
    zones: list[Zone],
) -> list[tuple[Zone, RRset]]:
    out: list[tuple[Zone, RRset]] = []
    for z in zones:
        try:
            for rs in client.list_rrsets(z.id):
                out.append((z, rs))
        except HetznerDnsError as e:
            err_console.print(f"[red]skip zone {z.name}: {e}[/red]")
    return out


def zone_filter_option(f):
    f = click.option(
        "--ttl-max",
        "zone_ttl_max",
        type=int,
        default=None,
        help="Only zones with default TTL <= this value.",
    )(f)
    f = click.option(
        "--ttl-min",
        "zone_ttl_min",
        type=int,
        default=None,
        help="Only zones with default TTL >= this value.",
    )(f)
    f = click.option(
        "--ttl",
        "zone_ttl_eq",
        type=int,
        default=None,
        help="Only zones with this exact default TTL.",
    )(f)
    f = click.option(
        "--label-selector",
        "label_selector",
        default=None,
        help="Server-side label selector, e.g. 'env=prod' or 'team=infra,env!=staging'.",
    )(f)
    f = click.option(
        "--zone",
        "zone_glob",
        default=None,
        help="Filter zones by name (fnmatch, e.g. '*.example.com').",
    )(f)
    return f


def _validate_zone_ttl_filters(eq: int | None, lo: int | None, hi: int | None) -> None:
    if eq is not None and (lo is not None or hi is not None):
        die("--ttl is mutually exclusive with --ttl-min/--ttl-max.")


def record_filter_options(f):
    f = click.option(
        "--type",
        "rec_type",
        multiple=True,
        default=(),
        help="Record type filter, e.g. A. Repeatable or comma-separated: "
             "`--type A,AAAA` or `--type A --type AAAA`.",
    )(f)
    f = click.option("--name", "name_glob", default=None, help="Filter by record name (fnmatch).")(f)
    f = click.option("--value", "value_glob", default=None, help="Filter where any value matches (fnmatch).")(f)
    f = click.option(
        "--explicit-ttl",
        "explicit_ttl",
        is_flag=True,
        help="Only rrsets with an explicit per-rrset TTL (overriding the zone default).",
    )(f)
    f = click.option(
        "--inherited-ttl",
        "inherited_ttl",
        is_flag=True,
        help="Only rrsets that inherit TTL from the zone default.",
    )(f)
    f = zone_filter_option(f)
    return f


def _expand_types(rec_type: tuple[str, ...]) -> list[str] | None:
    """Flatten click's tuple into a list of types, splitting comma values.
    Returns None when no filter should be applied."""
    out: list[str] = []
    for t in rec_type:
        out.extend(s.strip() for s in t.split(",") if s.strip())
    return out or None


def _ttl_state(explicit: bool, inherited: bool) -> str | None:
    if explicit and inherited:
        die("--explicit-ttl and --inherited-ttl are mutually exclusive.")
    if explicit:
        return "explicit"
    if inherited:
        return "inherited"
    return None


def mutation_options(f):
    f = click.option(
        "--log-file",
        "log_file",
        type=click.Path(dir_okay=False, path_type=Path),
        default=None,
        help="Append a JSON-line audit entry per rrset/zone attempt to this file.",
    )(f)
    f = click.option("--dry-run", is_flag=True, help="Show what would change without prompting or applying.")(f)
    f = click.option("--yes", "assume_yes", is_flag=True, help="Skip confirmation prompt.")(f)
    return f


@click.group()
@click.version_option(__version__, prog_name="hdns")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging (HTTP requests etc.) on stderr.")
def main(verbose: bool) -> None:
    """Hetzner DNS CLI (Hetzner Cloud DNS API)."""
    setup_logging(verbose)


@main.group()
def zones() -> None:
    """Operate on DNS zones."""


@zones.command("list")
@zone_filter_option
@click.option("--format", "fmt", type=click.Choice(["table", "json", "names"]), default="table")
def zones_list(
    zone_glob: str | None,
    label_selector: str | None,
    zone_ttl_eq: int | None,
    zone_ttl_min: int | None,
    zone_ttl_max: int | None,
    fmt: str,
) -> None:
    """List all zones in the account/project."""
    zs = _fetch_zones(
        _client(), zone_glob, label_selector=label_selector,
        ttl=zone_ttl_eq, ttl_min=zone_ttl_min, ttl_max=zone_ttl_max,
    )
    if fmt == "json":
        click.echo(json.dumps([z.raw for z in zs], indent=2))
        return
    if fmt == "names":
        for z in zs:
            click.echo(z.name)
        return
    table = Table(title=f"Zones ({len(zs)})")
    table.add_column("Name")
    table.add_column("ID")
    table.add_column("Default TTL", justify="right")
    table.add_column("Records", justify="right")
    table.add_column("Labels")
    for z in zs:
        table.add_row(z.name, str(z.id), str(z.ttl), str(z.record_count), _fmt_labels(z.labels))
    console.print(table)


def _fmt_labels(labels: dict[str, str]) -> str:
    if not labels:
        return "—"
    return ", ".join(f"{k}={v}" for k, v in sorted(labels.items()))


@zones.command("dump")
@zone_filter_option
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("./zonefiles"),
    show_default=True,
    help="Directory to write zonefiles into.",
)
@click.option("--force", is_flag=True, help="Overwrite existing files without prompting.")
def zones_dump(
    zone_glob: str | None,
    label_selector: str | None,
    zone_ttl_eq: int | None,
    zone_ttl_min: int | None,
    zone_ttl_max: int | None,
    out_dir: Path,
    force: bool,
) -> None:
    """Export each zone as a BIND-format zonefile and save it under OUT/<zone>.zone."""
    client = _client()
    zs = _fetch_zones(
        client, zone_glob, label_selector=label_selector,
        ttl=zone_ttl_eq, ttl_min=zone_ttl_min, ttl_max=zone_ttl_max,
    )
    if not zs:
        console.print("[yellow]No zones matched.[/yellow]")
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = [out_dir / f"{z.name}.zone" for z in zs if (out_dir / f"{z.name}.zone").exists()]
    if existing and not force:
        console.print(f"[yellow]{len(existing)} file(s) already exist in {out_dir}.[/yellow]")
        if not click.confirm("Overwrite them?", default=False):
            console.print("Aborted.")
            return
    written = 0
    for z in zs:
        try:
            content = client.export_zonefile(z.id)
        except HetznerDnsError as e:
            err_console.print(f"[red]{z.name}: {e}[/red]")
            continue
        path = out_dir / f"{z.name}.zone"
        path.write_text(content, encoding="utf-8")
        written += 1
        console.print(f"[green]wrote[/green] {path}")
    console.print(f"[bold]{written}/{len(zs)} zonefile(s) written to {out_dir}[/bold]")


@zones.command("set-ttl")
@click.argument("ttl", type=int)
@zone_filter_option
@mutation_options
def zones_set_ttl(
    ttl: int,
    zone_glob: str | None,
    label_selector: str | None,
    zone_ttl_eq: int | None,
    zone_ttl_min: int | None,
    zone_ttl_max: int | None,
    dry_run: bool,
    assume_yes: bool,
    log_file: Path | None,
) -> None:
    """Set the *zone-level* default TTL on every matching zone."""
    if ttl < 30:
        die(f"refusing TTL < 30s ({ttl}). Pick a saner value.")
    client = _client()
    zs = _fetch_zones(
        client, zone_glob, label_selector=label_selector,
        ttl=zone_ttl_eq, ttl_min=zone_ttl_min, ttl_max=zone_ttl_max,
    )
    targets = [z for z in zs if z.ttl != ttl]
    changes = [
        Change(target=f"zone {z.name}", field="ttl", old=str(z.ttl), new=str(ttl))
        for z in targets
    ]
    if not confirm_or_abort(
        changes,
        title=f"Set zone TTL to {ttl}",
        dry_run=dry_run,
        assume_yes=assume_yes,
    ):
        return
    ok = 0
    failed = 0
    with AuditLogger(log_file) as audit:
        for z in targets:
            try:
                action = client.change_zone_ttl(z.id, ttl=ttl)
                ok += 1
                action_id = action.get("id")
                console.print(
                    f"[green]zone ttl action queued[/green] {z.name}: "
                    f"{z.ttl} -> {ttl} (action {action_id})"
                )
                audit.record(
                    "zones.set-ttl",
                    zone=z.name,
                    record_id=str(z.id),
                    before={"ttl": z.ttl},
                    after={"ttl": ttl, "action_id": action_id},
                    result="ok",
                )
            except HetznerDnsError as e:
                failed += 1
                err_console.print(f"[red]{z.name}: {e} ({e.body or ''})[/red]")
                audit.record(
                    "zones.set-ttl",
                    zone=z.name,
                    record_id=str(z.id),
                    before={"ttl": z.ttl},
                    after={"ttl": ttl},
                    result="failed",
                    error=str(e),
                )
    console.print(f"[bold]{ok}/{len(changes)} zone ttl action(s) submitted (async)[/bold]")
    if failed:
        err_console.print(f"[bold red]{failed} zone ttl action(s) failed to submit[/bold red]")
        sys.exit(2)


@main.group()
def records() -> None:
    """Operate on DNS records (backed by rrsets in the Cloud API)."""


@records.command("list")
@record_filter_options
@click.option("--format", "fmt", type=click.Choice(["table", "json", "csv"]), default="table")
def records_list(
    zone_glob: str | None,
    label_selector: str | None,
    zone_ttl_eq: int | None,
    zone_ttl_min: int | None,
    zone_ttl_max: int | None,
    rec_type: tuple[str, ...],
    name_glob: str | None,
    value_glob: str | None,
    explicit_ttl: bool,
    inherited_ttl: bool,
    fmt: str,
) -> None:
    """List records, flattened from rrsets (one row per value)."""
    types = _expand_types(rec_type)
    ttl_state = _ttl_state(explicit_ttl, inherited_ttl)
    client = _client()
    zs = _fetch_zones(
        client, zone_glob, label_selector=label_selector,
        ttl=zone_ttl_eq, ttl_min=zone_ttl_min, ttl_max=zone_ttl_max,
    )
    pairs = _fetch_all_rrsets(client, zs)
    filtered = [(z, rs) for z, rs in pairs if filter_rrsets(
        [rs], type=types, name_glob=name_glob, value_glob=value_glob, ttl_state=ttl_state,
    )]

    if fmt == "json":
        # `rs.raw["zone"]` is the integer zone ID from the API; expose the
        # friendly name under a distinct key so we don't clobber it.
        click.echo(json.dumps([
            {**rs.raw, "zone_name": z.name} for z, rs in filtered
        ], indent=2))
        return

    # Flatten: one row per (zone, rrset, value) for table/csv.
    flat: list[tuple[Zone, RRset, str, str]] = []
    for z, rs in filtered:
        if not rs.records:
            flat.append((z, rs, "", ""))
        for rec in rs.records:
            flat.append((z, rs, rec.get("value", ""), rec.get("comment", "")))

    if fmt == "csv":
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(["zone", "name", "type", "value", "ttl", "ttl_inherited", "rrset_id", "comment"])
        for z, rs, value, comment in flat:
            ttl = rs.ttl if rs.ttl is not None else z.ttl
            inherited = "true" if rs.ttl is None else "false"
            writer.writerow([z.name, rs.name, rs.type, value, ttl, inherited, rs.id, comment])
        return

    table = Table(
        title=f"Records ({len(flat)} values across {len(filtered)} rrsets) "
              "— TTL marked * is inherited from the zone default",
    )
    table.add_column("Zone")
    table.add_column("Name")
    table.add_column("Type")
    table.add_column("Value", overflow="fold")
    table.add_column("TTL", justify="right")
    table.add_column("Lock", justify="center")
    for z, rs, value, _comment in flat:
        lock = "🔒" if rs.protection_change else ""
        if rs.ttl is None:
            ttl_display = f"{z.ttl}[dim]*[/dim]"
        else:
            ttl_display = str(rs.ttl)
        table.add_row(z.name, rs.name, rs.type, value or "(empty)", ttl_display, lock)
    console.print(table)


@records.command("set-ttl")
@click.argument("ttl")
@record_filter_options
@mutation_options
def records_set_ttl(
    ttl: str,
    zone_glob: str | None,
    label_selector: str | None,
    zone_ttl_eq: int | None,
    zone_ttl_min: int | None,
    zone_ttl_max: int | None,
    rec_type: tuple[str, ...],
    name_glob: str | None,
    value_glob: str | None,
    explicit_ttl: bool,
    inherited_ttl: bool,
    dry_run: bool,
    assume_yes: bool,
    log_file: Path | None,
) -> None:
    """Set per-rrset TTL on rrsets matching the filters.

    TTL is per-rrset in the new API: all values sharing a (name, type) share
    one TTL. Pass an integer (seconds) to set an explicit TTL, or the literal
    word `default` (alias: `inherit`) to clear the per-rrset TTL and fall back
    to the zone's default.

    Examples:
        hdns records set-ttl 3600 --type A
        hdns records set-ttl default --type NS
    """
    if ttl.lower() in ("default", "inherit", "null"):
        target_ttl: int | None = None
    else:
        try:
            target_ttl = int(ttl)
        except ValueError:
            die(f"TTL must be an integer or `default`, got {ttl!r}")
            return  # unreachable
        if target_ttl < 30:
            die(f"refusing TTL < 30s ({target_ttl}). Pick a saner value.")
    types = _expand_types(rec_type)
    ttl_state = _ttl_state(explicit_ttl, inherited_ttl)
    client = _client()
    zs = _fetch_zones(
        client, zone_glob, label_selector=label_selector,
        ttl=zone_ttl_eq, ttl_min=zone_ttl_min, ttl_max=zone_ttl_max,
    )
    pairs = _fetch_all_rrsets(client, zs)
    targets: list[tuple[Zone, RRset]] = []
    locked = 0
    for z, rs in pairs:
        if not filter_rrsets(
            [rs], type=types, name_glob=name_glob, value_glob=value_glob, ttl_state=ttl_state,
        ):
            continue
        if rs.ttl == target_ttl:
            continue
        if rs.protection_change:
            locked += 1
            continue
        targets.append((z, rs))
    if locked:
        err_console.print(f"[yellow]skipping {locked} rrset(s) with protection.change=true[/yellow]")

    def _fmt(v: int | None, zone_default: int) -> str:
        return f"{zone_default} (default)" if v is None else str(v)

    changes = [
        Change(
            target=f"{z.name} {rs.type} {rs.name}",
            field="ttl",
            old=_fmt(rs.ttl, z.ttl),
            new=_fmt(target_ttl, z.ttl),
        )
        for z, rs in targets
    ]
    title = "Set rrset TTL to default" if target_ttl is None else f"Set rrset TTL to {target_ttl}"
    if not confirm_or_abort(
        changes,
        title=title,
        dry_run=dry_run,
        assume_yes=assume_yes,
    ):
        return
    with AuditLogger(log_file) as audit:
        _apply_ttl_updates(client, targets, target_ttl, audit)


@records.command("replace-ip")
@click.argument("old_ip")
@click.argument("new_ip")
@record_filter_options
@click.option(
    "--substring",
    is_flag=True,
    help="Also replace substring matches inside values (e.g. TXT/SPF). Without "
         "this flag, only values exactly equal to OLD_IP are touched.",
)
@mutation_options
def records_replace_ip(
    old_ip: str,
    new_ip: str,
    zone_glob: str | None,
    label_selector: str | None,
    zone_ttl_eq: int | None,
    zone_ttl_min: int | None,
    zone_ttl_max: int | None,
    rec_type: tuple[str, ...],
    name_glob: str | None,
    value_glob: str | None,
    explicit_ttl: bool,
    inherited_ttl: bool,
    substring: bool,
    dry_run: bool,
    assume_yes: bool,
    log_file: Path | None,
) -> None:
    """Rewrite OLD_IP to NEW_IP across rrset values.

    By default this is an exact-value match against A (IPv4) or AAAA (IPv6)
    rrsets and `--type` is auto-inferred from OLD_IP's family. Use
    `--substring` to also rewrite occurrences inside other types' values such
    as TXT records carrying SPF strings.

    Within an rrset that has multiple values, only matching values are
    rewritten — the rest are preserved.
    """
    try:
        old_family = classify_ip(old_ip)
        new_family = classify_ip(new_ip)
    except ValueError as e:
        die(f"invalid IP: {e}")
        return  # unreachable

    if old_family != new_family and not substring:
        die(
            f"OLD_IP is {old_family} but NEW_IP is {new_family}. "
            "Mixing address families requires --substring."
        )

    types = _expand_types(rec_type)
    if not substring and types is None:
        # Exact-match mode: restrict to the IP's address family by default.
        types = [old_family]

    if substring:
        boundary = r"[0-9a-fA-F:]" if old_family == "AAAA" else r"[\d.]"
        pattern = re.compile(rf"(?<!{boundary}){re.escape(old_ip)}(?!{boundary})")
    else:
        pattern = None

    client = _client()
    zs = _fetch_zones(
        client, zone_glob, label_selector=label_selector,
        ttl=zone_ttl_eq, ttl_min=zone_ttl_min, ttl_max=zone_ttl_max,
    )
    pairs = _fetch_all_rrsets(client, zs)
    ttl_state = _ttl_state(explicit_ttl, inherited_ttl)
    matched = [(z, rs) for z, rs in pairs if filter_rrsets(
        [rs], type=types, name_glob=name_glob, value_glob=value_glob, ttl_state=ttl_state,
    )]

    plan: list[tuple[Zone, RRset, list[dict[str, str]], list[str], list[str]]] = []
    locked = 0
    for z, rs in matched:
        new_records = []
        changed_old: list[str] = []
        changed_new: list[str] = []
        for rec in rs.records:
            v = rec.get("value", "")
            new_v = v
            if substring and pattern.search(v):
                new_v = pattern.sub(new_ip, v)
            elif not substring and v == old_ip:
                new_v = new_ip
            if new_v != v:
                changed_old.append(v)
                changed_new.append(new_v)
            new_records.append({**rec, "value": new_v})
        if not changed_old:
            continue
        if rs.protection_change:
            locked += 1
            continue
        plan.append((z, rs, new_records, changed_old, changed_new))
    if locked:
        err_console.print(f"[yellow]skipping {locked} rrset(s) with protection.change=true[/yellow]")

    changes = []
    for z, rs, _new_records, olds, news in plan:
        for o, n in zip(olds, news):
            changes.append(Change(
                target=f"{z.name} {rs.type} {rs.name}",
                field="value",
                old=o,
                new=n,
            ))

    extra = None
    if substring:
        extra = ("Substring mode: matches are bounded by non-IP chars but you should "
                 "still eyeball the diff carefully, especially for TXT records.")
    if not confirm_or_abort(
        changes,
        title=f"Replace {old_ip} -> {new_ip}",
        dry_run=dry_run,
        assume_yes=assume_yes,
        extra_warning=extra,
    ):
        return

    with AuditLogger(log_file) as audit:
        _apply_records_updates(
            client,
            [(z, rs, new_records) for z, rs, new_records, _o, _n in plan],
            audit,
        )


def _apply_ttl_updates(
    client: HetznerDnsClient,
    targets: list[tuple[Zone, RRset]],
    ttl: int | None,
    audit: AuditLogger,
) -> None:
    if not targets:
        return
    new_ttl_str = "default" if ttl is None else str(ttl)
    ok = 0
    failed: list[dict] = []
    for z, rs in targets:
        old_ttl_str = "default" if rs.ttl is None else str(rs.ttl)
        try:
            action = client.change_rrset_ttl(z.id, rs.name, rs.type, ttl=ttl)
            ok += 1
            action_id = action.get("id")
            console.print(
                f"[green]ttl action queued[/green] {z.name} {rs.id}: "
                f"{old_ttl_str} -> {new_ttl_str} (action {action_id})"
            )
            audit.record(
                "records.set-ttl",
                zone=z.name,
                record_id=rs.id,
                type=rs.type,
                name=rs.name,
                before={"ttl": rs.ttl},
                after={"ttl": ttl, "action_id": action_id},
                result="ok",
            )
        except HetznerDnsError as e:
            err_console.print(f"[red]{z.name} {rs.id}: {e} ({e.body or ''})[/red]")
            failed.append({"rrset": rs.id, "zone": z.name, "error": str(e)})
            audit.record(
                "records.set-ttl",
                zone=z.name,
                record_id=rs.id,
                type=rs.type,
                name=rs.name,
                before={"ttl": rs.ttl},
                after={"ttl": ttl},
                result="failed",
                error=str(e),
            )
    console.print(f"[bold green]{ok}[/bold green] rrset ttl action(s) submitted (async)")
    if failed:
        err_console.print(f"[bold red]{len(failed)} action(s) failed to submit[/bold red]")
        sys.exit(2)


def _apply_records_updates(
    client: HetznerDnsClient,
    plan: list[tuple[Zone, RRset, list[dict[str, str]]]],
    audit: AuditLogger,
) -> None:
    """POST set_records action for each rrset; the action is async so we record
    submission success/failure, not eventual consistency."""
    if not plan:
        return
    ok = 0
    failed: list[dict] = []
    for z, rs, new_records in plan:
        try:
            action = client.set_rrset_records(z.id, rs.name, rs.type, new_records)
            ok += 1
            action_id = action.get("id")
            console.print(f"[green]records action queued[/green] {z.name} {rs.id} (action {action_id})")
            audit.record(
                "records.replace-ip",
                zone=z.name,
                record_id=rs.id,
                type=rs.type,
                name=rs.name,
                before={"records": rs.records},
                after={"records": new_records, "action_id": action_id},
                result="ok",
            )
        except HetznerDnsError as e:
            err_console.print(f"[red]{z.name} {rs.id}: {e} ({e.body or ''})[/red]")
            failed.append({"rrset": rs.id, "zone": z.name, "error": str(e)})
            audit.record(
                "records.replace-ip",
                zone=z.name,
                record_id=rs.id,
                type=rs.type,
                name=rs.name,
                before={"records": rs.records},
                after={"records": new_records},
                result="failed",
                error=str(e),
            )
    console.print(f"[bold green]{ok}[/bold green] rrset action(s) submitted (async)")
    if failed:
        err_console.print(f"[bold red]{len(failed)} rrset action(s) failed to submit[/bold red]")
        sys.exit(2)


if __name__ == "__main__":
    main()
