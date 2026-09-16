"""The endpoints a team has marked as ones that must not break.

Kerno's portal owns that decision. This module is how the check learns it, from either of the two
places it can be read in CI:

* every ``<app>/.kerno/criticality.json`` in the checkout, written by the agent on its last sync —
  needs no account, no secret and no network, which is why it is the default; and
* events-service, when ``api-key``/``organization-id`` are set — always current, never stale.

Live wins when it is available, because a mark made an hour ago should count. But a live fetch that
cannot answer falls back to the file and warns: a check that goes red because the portal blinked is
a check people learn to ignore.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.parse import quote

from portal import HttpCall, PortalConfig, default_http_get, json_headers

KERNO_DIR = ".kerno"
CRITICALITY_FILE_NAME = "criticality.json"

SOURCE_LIVE = "live"
SOURCE_FILE = "file"
SOURCE_NONE = "none"


@dataclass(frozen=True)
class CriticalEndpoint:
    """One endpoint that must not break.

    ``filepath`` may be empty — an endpoint whose location Kerno never recorded. It is kept rather
    than dropped so a reviewer can be told "critical endpoint with no known location" instead of it
    silently not counting.
    """

    content_root: str
    method: str
    url_path: str
    filepath: str

    @property
    def label(self) -> str:
        return f"{self.method} {self.url_path}"


@dataclass(frozen=True)
class CriticalitySet:
    """What this run believes is critical, and where that belief came from.

    The source is carried, not inferred. "0 critical endpoints" read live from the portal and "0"
    read from a file the agent last wrote three weeks ago are the same number and very different
    facts, and a reviewer reading the summary is entitled to know which one they are looking at.
    """

    endpoints: tuple[CriticalEndpoint, ...]
    source: str
    stale_reason: str = ""


def _parse_document(raw: str, fallback_content_root: str) -> list[CriticalEndpoint]:
    document = json.loads(raw)
    entries = document.get("endpoints") or []
    parsed = []
    for entry in entries:
        method = str(entry.get("method", "")).strip().upper()
        url_path = str(entry.get("urlPath", "")).strip()
        if not method or not url_path:
            continue
        parsed.append(
            CriticalEndpoint(
                # The entry's own content root is authoritative — it is the identity events-service
                # stored the mark under. The directory the file was found in is only a fallback for
                # a file written before that field existed.
                content_root=str(entry.get("contentRoot", fallback_content_root) or ""),
                method=method,
                url_path=url_path,
                filepath=str(entry.get("filepath", "") or ""),
            )
        )
    return parsed


def load_from_files(workspace: str) -> list[CriticalEndpoint]:
    """Every marked endpoint in the checkout, from each app's own criticality file.

    Discovered rather than configured, the same way the replay step finds every ``.kerno/scenarios``
    tree: a monorepo has one file per application and the action is given no list of them.
    """
    found: list[CriticalEndpoint] = []
    for root, dirs, files in os.walk(workspace):
        # Nothing Kerno writes lives under these, and walking a node_modules tree in a large
        # monorepo costs more than the rest of this step put together.
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "vendor", "target"}]
        if os.path.basename(root) != KERNO_DIR or CRITICALITY_FILE_NAME not in files:
            continue
        path = os.path.join(root, CRITICALITY_FILE_NAME)
        app_dir = os.path.relpath(os.path.dirname(root), workspace)
        content_root = "" if app_dir == "." else app_dir
        try:
            with open(path, encoding="utf-8") as handle:
                found.extend(_parse_document(handle.read(), content_root))
        except (OSError, json.JSONDecodeError, AttributeError) as error:
            # One unreadable file must not hide the marks of every other application in the
            # repository, and it must certainly not fail the check.
            print(f"::warning::could not read {path}: {error}")
    return found


def fetch_live(
    config: PortalConfig,
    *,
    http_get: HttpCall = default_http_get,
) -> list[CriticalEndpoint] | None:
    """The marks as events-service holds them right now, or None when it could not answer.

    None and an empty list are different answers: empty means the organization has marked nothing,
    None means this run does not know — and only None falls back to the checkout.
    """
    url = (
        f"{config.events_url.rstrip('/')}"
        f"/organizations/{config.organization_id}/endpoints/critical"
        f"?gitRepo={quote(config.git_repo, safe='')}"
    )
    try:
        status, raw = http_get(url, json_headers(config.api_key), b"")
    except (OSError, TimeoutError, ValueError) as error:
        # ValueError is not paranoia: a 200 whose body is not UTF-8 — a proxy's error page, say —
        # raises UnicodeDecodeError out of the shared HTTP helper, and UnicodeDecodeError is a
        # ValueError, not an OSError. Uncaught it would fail a check whose tests all passed, which
        # is the one thing criticality must never do.
        print(f"::warning::could not read critical endpoints from the portal: {error}")
        return None
    if status >= 300:
        print(f"::warning::could not read critical endpoints from the portal (HTTP {status})")
        return None
    try:
        rows = json.loads(raw)
    except ValueError as error:
        print(f"::warning::the portal's critical endpoints were not readable: {error}")
        return None
    if not isinstance(rows, list):
        print("::warning::the portal's critical endpoints were not a list")
        return None
    return [
        CriticalEndpoint(
            content_root=str(row.get("contentRoot", "") or ""),
            method=str(row.get("method", "")).strip().upper(),
            url_path=str(row.get("urlPath", "")).strip(),
            filepath=str(row.get("filepath", "") or ""),
        )
        for row in rows
        if str(row.get("method", "")).strip() and str(row.get("urlPath", "")).strip()
    ]


def load_criticality(
    workspace: str,
    config: PortalConfig | None,
    *,
    http_get: HttpCall = default_http_get,
) -> CriticalitySet:
    """The marks for this run, preferring the portal and falling back to the checkout."""
    if config is not None:
        live = fetch_live(config, http_get=http_get)
        if live is not None:
            return CriticalitySet(endpoints=tuple(live), source=SOURCE_LIVE)
        from_files = load_from_files(workspace)
        return CriticalitySet(
            endpoints=tuple(from_files),
            source=SOURCE_FILE,
            stale_reason="the portal could not be reached",
        )

    from_files = load_from_files(workspace)
    if not from_files:
        # No account and no file: the repository has never been synced by an agent that knew about
        # criticality. Absent, not empty — and the check says nothing at all rather than implying
        # someone looked and found nothing.
        return CriticalitySet(endpoints=(), source=SOURCE_NONE)
    return CriticalitySet(endpoints=tuple(from_files), source=SOURCE_FILE)


def describe(criticality: CriticalitySet) -> str:
    """One line for the step summary, naming the count AND where it came from."""
    if criticality.source == SOURCE_NONE:
        # Nothing has ever told this repository what is critical. Saying "0 critical endpoints"
        # would claim somebody looked, so the caller is expected to stay quiet instead.
        return "not known for this repository"
    count = len(criticality.endpoints)
    noun = "endpoint" if count == 1 else "endpoints"
    if criticality.source == SOURCE_LIVE:
        return f"{count} critical {noun} (from the portal)"
    if criticality.stale_reason:
        return f"{count} critical {noun} (from the checkout — {criticality.stale_reason})"
    return f"{count} critical {noun} (from the checkout)"
