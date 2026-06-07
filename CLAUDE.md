# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`hdns` is a Python CLI for the **Hetzner Cloud DNS API** (`api.hetzner.cloud/v1`).
Tokens come from the Hetzner Cloud Console (project → Security → API tokens)
and live in `HCLOUD_TOKEN` (same env var as the `hcloud` CLI / Terraform
provider). Tokens are project-scoped.

## Dev loop

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .          # editable install; edits under hdns/ are picked up live
export HCLOUD_TOKEN=...   # also in .env
hdns -v zones list        # -v turns on HTTP debug logging on stderr
```

No test suite, linter, or formatter is configured. There is no CI. Verification
is by running the CLI against the live API. Most read-only verification can be
done with `--help`, `--dry-run`, or by invoking pure helpers directly
(`python -c "from hdns.utils import classify_ip; ..."`).

When iterating against the live API, the `--dry-run` flag on any mutating
command shows the proposed Change table without prompting or writing — use it
liberally before flipping to `--yes`.

## API gotchas this code was built around

These are not obvious from the Hetzner docs and were discovered by probing.
Preserve the workarounds when touching `client.py`:

1. **RRset model.** Records sharing `(name, type)` are one RRset object with a
   single TTL and a `records: [{value, comment}, ...]` list. The CLI flattens
   for display but every mutation is at the rrset level. RRset `id` is the
   string `"name/type"`, not an integer.

2. **PUT silently 200s on TTL/record changes.** `PUT /v1/zones/{id}/rrsets/{name}/{type}`
   exists but only updates labels; sending `{"ttl": N}` returns 200 with the
   *unchanged* value. Same trap on `PUT /v1/zones/{id}` for the zone-level
   default TTL. The action endpoints are the only real write path:
   - `POST /v1/zones/{id}/actions/change_ttl`
   - `POST /v1/zones/{id}/rrsets/{name}/{type}/actions/change_ttl`
   - `POST /v1/zones/{id}/rrsets/{name}/{type}/actions/set_records`

3. **All mutations are async.** Action endpoints return 201 with an
   `{action: {id, status: "running", command, ...}}` envelope. `hdns` records
   the `action_id` in the audit log and exits — it does **not** poll. In
   practice actions finish well under a second.

4. **`ttl=null` clears a per-rrset override** and falls back to the zone
   default. `records set-ttl default` (also `inherit`/`null`) sends `{"ttl": null}`.

5. **Zonefile endpoint** is `GET /zones/{id}/zonefile` and returns
   `{"zonefile": "..."}` (JSON-wrapped, not plain text). Not `/export`.

The HTTP layer in `client.py` logs full response bodies for mutating verbs at
DEBUG level specifically so silent-200 bugs surface. Don't remove that.
`allow_redirects=False` is intentional — a redirect on this API generally means
the request is hitting the wrong base URL.

## Architecture

Three modules, single responsibility each:

- **`hdns/client.py`** — `HetznerDnsClient` (thin requests wrapper),
  `Zone` / `RRset` / `HetznerDnsError` dataclasses. The only place that talks
  HTTP. Returns dataclasses, not raw JSON, except `.raw` carries the dict for
  `--format json`.

- **`hdns/utils.py`** — pure helpers: `filter_zones` / `filter_rrsets`
  (client-side composable filters), `classify_ip`, `Change` dataclass +
  `render_changes` (Rich table for previews), `confirm_or_abort` (the standard
  preview-and-prompt flow with 25-row "type the number" friction),
  `AuditLogger` (no-op when path is None, append-only JSONL, context-managed).

- **`hdns/cli.py`** — click command tree. All commands compose the same filter
  set via two decorators:
  - `zone_filter_option` adds `--zone`, `--label-selector`, `--ttl`,
    `--ttl-min`, `--ttl-max` and threads them through `_fetch_zones`.
  - `record_filter_options` builds on it and adds `--type` (repeatable +
    comma-splittable via `_expand_types`), `--name`, `--value`, `--explicit-ttl`,
    `--inherited-ttl` (mutex via `_ttl_state`).

  When adding a new command that walks zones or rrsets, apply these decorators
  and route through `_fetch_zones` / `_fetch_all_rrsets` so filters compose
  uniformly across the whole CLI.

### Mutation flow

Every mutating command follows the same shape — keep this consistent:

1. Build a list of `Change` rows describing each proposed mutation.
2. Pass to `confirm_or_abort` (handles dry-run, --yes, large-batch friction).
3. Inside an `AuditLogger` context, apply changes one at a time; record both
   `ok` and `failed` entries with `before` / `after` / `action_id`.
4. Print a summary line; exit 2 if any mutations failed to submit.

`protection.change=true` rrsets are filtered out *before* building the change
list (so the preview doesn't show them) and the skip count is reported on
stderr.

### TTL display convention

`zones list` shows the zone's "Default TTL". `records list` shows the *effective*
TTL: if the rrset has an explicit `ttl`, it's printed as-is; if it's inheriting
the zone default, it's printed as `<zone_default>*` (CSV output splits this
into separate `ttl` and `ttl_inherited` columns). Preserve this — the `*` is
the only on-screen signal that an rrset is inheriting vs. overriding.

## Style notes from prior collaboration

- No `# ---- section divider ----` style comments anywhere in the code.
- Comments only when the *why* is non-obvious (API quirks, silent-200 traps);
  don't narrate what well-named code already shows.
