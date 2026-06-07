# hdns

CLI for the Hetzner Cloud DNS API (`api.hetzner.cloud/v1`).

> **Note:** this project was mainly AI-generated (Claude Code). The code, docs,
> and the API gotchas it works around were produced through iterative prompting
> against the live Hetzner API. Treat it as a useful tool, not a vetted library
> — read the diff before you `--yes` a bulk mutation.

## Auth

Create a token in the Hetzner Cloud Console: select a project → **Security** →
**API tokens** → *Generate API token*. The Hetzner Cloud CLI / Terraform
provider use the same token, so the env var name matches theirs:

```bash
export HCLOUD_TOKEN=...
```

Tokens are scoped to one project; if you have zones in multiple projects,
re-export the relevant token per session.

## Commands

```text
hdns zones list                      # list all zones
hdns zones dump --out ./zonefiles    # save BIND-format zonefiles locally
hdns zones set-ttl 3600              # set zone-level TTL across all zones
hdns records list                    # list records across all zones
hdns records set-ttl 3600 --type A   # set per-record TTL with filters
hdns records replace-ip 1.2.3.4 5.6.7.8   # rewrite a value (A/AAAA) across all zones
```

All mutating commands print a preview table and prompt for confirmation. Use
`--dry-run` to preview without prompting, or `--yes` to skip the prompt.

### Note on the new data model

The Cloud DNS API is **RRset-based**: records that share `(name, type)` live
inside one RRset object with a single TTL and a list of values. The CLI flattens
this for display (one row per value) but mutates at the rrset level:

- `records set-ttl` sets `rrset.ttl` (so co-located values share a TTL).
- `records replace-ip` rewrites just the matching value inside the rrset and
  leaves the others intact.
- An rrset with `protection.change=true` is skipped (and noted on stderr).

### Write path mapping

- `zones set-ttl`     → `POST /v1/zones/{id}/actions/change_ttl` with
                        `{"ttl": N}` — async, returns `command: change_zone_ttl`
                        (PUT on `/zones/{id}` silently 200s and ignores the
                        TTL field, same trap as rrsets)
- `records set-ttl`   → `POST /v1/zones/{id}/rrsets/{name}/{type}/actions/change_ttl`
                        with `{"ttl": N}` — async, returns `command:
                        change_rrset_ttl`
- `records replace-ip`→ `POST /v1/zones/{id}/rrsets/{name}/{type}/actions/set_records`
                        with the full `{"records": [...]}` list (matching values
                        rewritten, others preserved) — async, returns
                        `command: set_rrset_records`

Both rrset mutations are **async**: the API returns 201 with an action
object (`{id, status: "running", command, progress}`). `hdns` records the
submission in the audit log with the `action_id` and exits; it does not poll
for completion. In practice the action finishes well under a second; if you
need confirmation, `curl /v1/actions/{id}` to poll.

> The PUT endpoint on `/rrsets/{name}/{type}` exists but only updates labels;
> if you send `{"ttl": N}` it returns 200 with the *unchanged* TTL — silently
> ignoring the field. That's why TTL changes go via the action endpoint.

Filters available on `records list / set-ttl / replace-ip`:

- `--zone GLOB`   match zone name (fnmatch, e.g. `*.example.com`)
- `--type TYPE`   filter by record type (`A`, `AAAA`, `CNAME`, ...)
- `--name GLOB`   filter by record name
- `--value GLOB`  filter by record value

## Logging

- `hdns -v ...` enables debug logging to stderr (HTTP method, path, status,
  elapsed ms, response size; also urllib3's connection-level logs).
- `--log-file PATH` on any mutating command appends one JSON line per
  record/zone attempt to that file. Each line has `ts`, `op`, `zone`,
  `record_id`, `type`, `name`, `before`, `after`, `result` (`ok` / `failed`)
  and `error` on failures. Safe to tail, grep, or feed to `jq`.

```bash
hdns records set-ttl 3600 --type A --log-file audit.jsonl
jq -r 'select(.result=="failed") | "\(.zone) \(.name): \(.error)"' audit.jsonl
```

## Development setup

Requirements: Python 3.10+.

```bash
# clone the repo (already cloned if you're reading this in-tree)
cd hetzner-dns-cl

# create an isolated venv
python3 -m venv .venv
source .venv/bin/activate

# editable install pulls in click / requests / rich
pip install -e .

# point at your token
export HCLOUD_TOKEN=...

# smoke-test
hdns --help
hdns -v zones list        # -v shows the HTTP request line(s)
```

The package layout:

```
hetzner-dns-cl/
├── pyproject.toml        # setuptools, entry point `hdns`
├── README.md
└── hdns/
    ├── __init__.py       # version
    ├── __main__.py       # `python -m hdns` entry
    ├── cli.py            # click command tree
    ├── client.py         # thin requests-based Hetzner DNS client
    └── utils.py          # filters, IP helpers, AuditLogger, logging setup
```

While iterating you can re-run the CLI without reinstalling — the editable
install means edits to files under `hdns/` are picked up on the next
invocation. If you change `pyproject.toml` (e.g. adding a dep), re-run
`pip install -e .`.

For a quick interactive check that doesn't touch the API:

```bash
python -m hdns records replace-ip --help
python -m hdns zones dump --help
```

### Iterating without a token

Most read-only commands need a token, but the `--help` output, IP-classifier
unit logic, and substring-boundary regex can be exercised standalone:

```bash
python -c "from hdns.utils import classify_ip; print(classify_ip('2001:db8::1'))"
```
