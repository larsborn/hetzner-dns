"""Thin client around the Hetzner Cloud DNS API (api.hetzner.cloud/v1).

The new API is RRset-based: records sharing the same (name, type) live inside
one RRset object with a single TTL and a `records: [{value, comment}, ...]`
list. The CLI presents flattened records to the user but mutates at the rrset
level.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import requests


DEFAULT_BASE_URL = "https://api.hetzner.cloud/v1/"

log = logging.getLogger("hdns.http")


class HetznerDnsError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class Zone:
    id: int
    name: str
    ttl: int
    record_count: int = 0
    labels: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Zone":
        return cls(
            id=data["id"],
            name=data["name"],
            ttl=data.get("ttl", 0),
            record_count=data.get("record_count", 0),
            labels=data.get("labels") or {},
            raw=data,
        )


@dataclass
class RRset:
    id: str  # "name/type", e.g. "@/A"
    name: str
    type: str
    ttl: int | None  # None means "inherit zone default TTL"
    records: list[dict[str, str]]  # each item: {"value": str, "comment": str}
    zone_id: int
    protection_change: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "RRset":
        return cls(
            id=data["id"],
            name=data["name"],
            type=data["type"],
            ttl=data.get("ttl"),
            records=list(data.get("records") or []),
            zone_id=data.get("zone", 0),
            protection_change=bool((data.get("protection") or {}).get("change", False)),
            raw=data,
        )

    def values(self) -> list[str]:
        return [r.get("value", "") for r in self.records]


class HetznerDnsClient:
    def __init__(self, token: str, base_url: str = DEFAULT_BASE_URL, timeout: int = 30):
        if not token:
            raise ValueError("API token is required")
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        data: bytes | str | None = None,
        content_type: str | None = None,
        expect_json: bool = True,
    ) -> Any:
        url = self.base_url + path.lstrip("/")
        headers: dict[str, str] = {}
        if content_type:
            headers["Content-Type"] = content_type
        start = time.monotonic()
        resp = self._session.request(
            method,
            url,
            params=params,
            json=json,
            data=data,
            headers=headers or None,
            timeout=self.timeout,
            allow_redirects=False,
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        log.debug(
            "%s %s%s -> %d (%dms, %dB)",
            method,
            path,
            f"?{requests.compat.urlencode(params)}" if params else "",
            resp.status_code,
            elapsed_ms,
            len(resp.content or b""),
        )
        # For mutating verbs, also log the response body so we can confirm what
        # the API actually accepted (useful when a 200 doesn't mean the field
        # we sent was honored).
        if method in ("POST", "PUT", "PATCH", "DELETE") and resp.content:
            log.debug("response body: %s", resp.text[:1000])
        if resp.status_code >= 300:
            log.debug("response body: %s", resp.text[:1000])
            raise HetznerDnsError(
                f"{method} {path} returned {resp.status_code}",
                status=resp.status_code,
                body=resp.text,
            )
        if not expect_json:
            return resp.text
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def iter_zones(
        self,
        *,
        search: str | None = None,
        label_selector: str | None = None,
        per_page: int = 50,
    ) -> Iterator[Zone]:
        page = 1
        while True:
            params: dict[str, Any] = {"page": page, "per_page": per_page}
            if search:
                params["name"] = search
            if label_selector:
                params["label_selector"] = label_selector
            data = self._request("GET", "zones", params=params) or {}
            zones = data.get("zones") or []
            for z in zones:
                yield Zone.from_api(z)
            next_page = ((data.get("meta") or {}).get("pagination") or {}).get("next_page")
            if not next_page or not zones:
                return
            page = next_page

    def list_zones(self, *, search: str | None = None, label_selector: str | None = None) -> list[Zone]:
        return list(self.iter_zones(search=search, label_selector=label_selector))

    def get_zone(self, zone_id: int | str) -> Zone:
        data = self._request("GET", f"zones/{zone_id}")
        return Zone.from_api(data["zone"])

    def change_zone_ttl(self, zone_id: int | str, *, ttl: int) -> dict[str, Any]:
        """Change a zone's default TTL via the action endpoint.

        PUT on `/zones/{id}` with `{"ttl": N}` returns 200 but silently ignores
        the field — same trap as rrsets. The action endpoint actually applies
        the change. Async: returns an action dict.
        """
        data = self._request(
            "POST",
            f"zones/{zone_id}/actions/change_ttl",
            json={"ttl": ttl},
            content_type="application/json",
        )
        return data.get("action") or {}

    def export_zonefile(self, zone_id: int | str) -> str:
        """Return the BIND-format zonefile string for `zone_id`."""
        data = self._request("GET", f"zones/{zone_id}/zonefile") or {}
        if isinstance(data, dict) and "zonefile" in data:
            return data["zonefile"]
        # Fallback: API returned plain text (shouldn't happen based on probes).
        return str(data)

    def list_rrsets(self, zone_id: int | str, *, per_page: int = 100) -> list[RRset]:
        out: list[RRset] = []
        page = 1
        while True:
            data = self._request(
                "GET",
                f"zones/{zone_id}/rrsets",
                params={"page": page, "per_page": per_page},
            ) or {}
            rrsets = data.get("rrsets") or []
            for rs in rrsets:
                out.append(RRset.from_api(rs))
            next_page = ((data.get("meta") or {}).get("pagination") or {}).get("next_page")
            if not next_page or not rrsets:
                return out
            page = next_page

    def get_rrset(self, zone_id: int | str, name: str, type: str) -> RRset:
        data = self._request("GET", f"zones/{zone_id}/rrsets/{name}/{type}")
        return RRset.from_api(data["rrset"])

    def change_rrset_ttl(
        self,
        zone_id: int | str,
        name: str,
        type: str,
        *,
        ttl: int | None,
    ) -> dict[str, Any]:
        """Change an rrset's TTL via the `change_ttl` action endpoint.

        Pass `ttl=None` to clear the per-rrset TTL and fall back to the zone
        default. Async: returns an action dict. PUT on the rrset URL silently
        ignores TTL changes (it's for labels only), so we go through the
        action API just like set_records.
        """
        data = self._request(
            "POST",
            f"zones/{zone_id}/rrsets/{name}/{type}/actions/change_ttl",
            json={"ttl": ttl},
            content_type="application/json",
        )
        return data.get("action") or {}

    def set_rrset_records(
        self,
        zone_id: int | str,
        name: str,
        type: str,
        records: list[dict[str, str]],
    ) -> dict[str, Any]:
        """Replace an rrset's records via the `set_records` action endpoint.

        Returns the action dict (`{id, status, command, progress, ...}`). The
        action is async: a 201 response means the request was accepted, not
        that the change is visible yet. Callers can poll
        `GET /actions/{id}` to confirm completion.
        """
        data = self._request(
            "POST",
            f"zones/{zone_id}/rrsets/{name}/{type}/actions/set_records",
            json={"records": records},
            content_type="application/json",
        )
        return data.get("action") or {}
