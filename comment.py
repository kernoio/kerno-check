"""PR comment and job-summary table for a replay: totals, then one row per portal run.

Without portal URLs the comment is only the totals — a suite of 187 passing scenarios must not
become a 187-row table. The JUnit reporter can still create a check; this is the comment.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from portal import PortalRun

COMMENT_MARKER = "<!-- kerno-check -->"


def format_comment(
    *,
    passed: int,
    failed: int,
    skipped: int,
    runs: list[PortalRun],
) -> str:
    total = passed + failed + skipped
    lines = [
        COMMENT_MARKER,
        f"**Kerno check:** {passed} passed, {failed} failed, {skipped} skipped ({total} total)",
        "",
    ]
    if not runs:
        return "\n".join(lines)
    lines.extend(
        [
            "| Endpoint | Passed | Failed | Report |",
            "| --- | ---: | ---: | --- |",
        ]
    )
    for run in runs:
        label = f"{run.content_root} · {run.endpoint}" if run.content_root else run.endpoint
        label = label.replace("|", "\\|")
        lines.append(
            f"| `{label}` | {run.passed} | {run.failed} | [open]({run.portal_run_url}) |"
        )
    lines.append("")
    return "\n".join(lines)


def upsert_pull_request_comment(body: str) -> None:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if os.environ.get("GITHUB_EVENT_NAME", "") != "pull_request" or not token:
        return
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    number = _pull_request_number()
    if not repo or number is None:
        return
    api = f"https://api.github.com/repos/{repo}/issues/{number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "kerno-check",
    }
    try:
        existing = _find_comment(api, headers)
        if existing is None:
            status, _ = _http("POST", api, headers, {"body": body})
        else:
            status, _ = _http("PATCH", existing, headers, {"body": body})
        if status >= 300:
            print(f"::warning::could not post the Kerno check comment (HTTP {status})")
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as error:
        print(f"::warning::could not post the Kerno check comment: {error}")


def _pull_request_number() -> int | None:
    path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not path or not os.path.isfile(path):
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    number = payload.get("number") or (payload.get("pull_request") or {}).get("number")
    try:
        return int(number)
    except (TypeError, ValueError):
        return None


def _find_comment(list_url: str, headers: dict[str, str]) -> str | None:
    status, raw = _http("GET", f"{list_url}?per_page=100", headers)
    if status >= 300:
        print(f"::warning::could not list PR comments (HTTP {status})")
        return None
    for comment in json.loads(raw):
        if COMMENT_MARKER in (comment.get("body") or ""):
            url = comment.get("url")
            if isinstance(url, str) and url:
                return url
    return None


def _http(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None = None,
) -> tuple[int, str]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = dict(headers)
    if data is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.getcode(), response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")
