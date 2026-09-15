"""Open one portal run report per endpoint after replay, and return the URLs.

The payload is the runner's capture (`run-report.json`), not a reconstruction from JUnit.
JUnit is the CI gate; it cannot carry request/response fields, so inventing rows from it
would publish empty HTTP and a status of 0.

Best-effort: a down events-service must not fail the check. Unset credentials skip this
entirely, which is the no-account path the action advertises.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

DEFAULT_EVENTS_URL = "https://events.kerno.io/events-service/"
DEFAULT_PORTAL_URL = "https://portal.kerno.io"
VIRTUAL_KEY_HEADER = "x-kerno-virtual-key-id"

HttpCall = Callable[[str, dict[str, str], bytes], tuple[int, str]]


@dataclass(frozen=True)
class PortalConfig:
    api_key: str
    organization_id: str
    events_url: str
    portal_url: str
    git_repo: str
    git_branch: str
    commit_sha: str


@dataclass(frozen=True)
class PortalRun:
    endpoint: str
    content_root: str
    passed: int
    failed: int
    skipped: int
    run_report_id: str
    portal_run_url: str


def load_portal_config() -> PortalConfig | None:
    api_key = os.environ.get("KERNO_API_KEY", "").strip()
    organization_id = os.environ.get("KERNO_ORGANIZATION_ID", "").strip()
    if not api_key and not organization_id:
        return None
    if not api_key or not organization_id:
        raise ValueError(
            "api-key and organization-id must be set together — one without the other cannot open a portal run"
        )
    return PortalConfig(
        api_key=api_key,
        organization_id=organization_id,
        events_url=os.environ.get("KERNO_EVENTS_URL", "").strip() or DEFAULT_EVENTS_URL,
        portal_url=os.environ.get("KERNO_PORTAL_URL", "").strip() or DEFAULT_PORTAL_URL,
        git_repo=os.environ.get("GITHUB_REPOSITORY", "").strip() or "unknown/unknown",
        git_branch=(
            os.environ.get("GITHUB_HEAD_REF", "").strip()
            or os.environ.get("GITHUB_REF_NAME", "").strip()
            or "unknown"
        ),
        commit_sha=os.environ.get("GITHUB_SHA", "").strip(),
    )


def load_capture(path: str) -> list[dict[str, Any]] | None:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    scenarios = payload.get("scenarios") if isinstance(payload, dict) else None
    if not isinstance(scenarios, list):
        return None
    return [scenario for scenario in scenarios if isinstance(scenario, dict)]


def parse_endpoint(classname: str) -> tuple[str, str] | None:
    label = classname.strip()
    if not label:
        return None
    method, _, path = label.partition(" ")
    if not method:
        return None
    return method, (path.strip() or "/")


def group_by_endpoint(scenarios: list[dict[str, Any]]) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for scenario in scenarios:
        parsed = parse_endpoint(str(scenario.get("endpoint") or ""))
        if parsed is None:
            continue
        method, path = parsed
        grouped[(str(scenario.get("contentRoot") or ""), method, path)].append(scenario)
    return grouped


def portal_run_url(portal_url: str, organization_id: str, run_report_id: str) -> str:
    return f"{portal_url.rstrip('/')}/runs/{run_report_id}?org={organization_id}"


def default_http(url: str, headers: dict[str, str], body: bytes) -> tuple[int, str]:
    return _http("POST", url, headers, body)


def default_http_patch(url: str, headers: dict[str, str], body: bytes) -> tuple[int, str]:
    return _http("PATCH", url, headers, body)


def _http(method: str, url: str, headers: dict[str, str], body: bytes) -> tuple[int, str]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.getcode(), response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")


def _json_headers(api_key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        VIRTUAL_KEY_HEADER: api_key,
    }


def _counts(group: list[dict[str, Any]]) -> tuple[int, int, int, int]:
    passed = failed = skipped = diffs = 0
    for scenario in group:
        status = scenario.get("status")
        if status == "failed":
            failed += 1
        elif status == "skipped":
            skipped += 1
        else:
            passed += 1
        row = scenario.get("row")
        if isinstance(row, dict) and row.get("state") == "diff_detected":
            diffs += 1
    return passed, failed, skipped, diffs


def _rows(group: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for scenario in group:
        row = scenario.get("row")
        if isinstance(row, dict):
            rows.append(row)
    return rows


def publish_runs(
    scenarios: list[dict[str, Any]],
    config: PortalConfig,
    *,
    http_post: HttpCall = default_http,
    http_patch: HttpCall = default_http_patch,
) -> list[PortalRun]:
    events_base = config.events_url.rstrip("/")
    published: list[PortalRun] = []
    for (content_root, method, path), group in sorted(group_by_endpoint(scenarios).items()):
        rows = _rows(group)
        if not rows:
            continue
        endpoint = f"{method} {path}"
        start_body = json.dumps(
            {
                "gitRepo": config.git_repo,
                "gitBranch": config.git_branch,
                "contentRoot": content_root,
                "method": method,
                "urlPath": path,
                "commitSha": config.commit_sha or None,
                "status": "running",
            }
        ).encode("utf-8")
        start_url = f"{events_base}/organizations/{config.organization_id}/run-reports"
        try:
            status, raw = http_post(start_url, _json_headers(config.api_key), start_body)
        except (OSError, TimeoutError) as error:
            print(f"::warning::could not open a portal run for {endpoint}: {error}")
            continue
        if status >= 300:
            print(f"::warning::could not open a portal run for {endpoint} (HTTP {status})")
            continue
        try:
            run_report_id = json.loads(raw).get("id")
        except json.JSONDecodeError:
            run_report_id = None
        if not run_report_id:
            print(f"::warning::events-service opened {endpoint} without an id")
            continue

        passed, failed, skipped, diffs = _counts(group)
        finish_body = json.dumps(
            {
                "status": "completed",
                "outcome": "diffs_rejected" if diffs else "no_diffs",
                "totalScenarios": len(rows),
                "diffsDetected": diffs,
                "scenariosAdded": 0,
                "scenariosUpdated": 0,
                "scenariosRemoved": 0,
                "report": {"scenarios": rows},
            }
        ).encode("utf-8")
        finish_url = (
            f"{events_base}/organizations/{config.organization_id}/run-reports/{run_report_id}"
        )
        try:
            finish_status, _ = http_patch(finish_url, _json_headers(config.api_key), finish_body)
        except (OSError, TimeoutError) as error:
            print(
                f"::warning::opened {endpoint} as {run_report_id} but could not finish it: {error}"
            )
            continue
        if finish_status >= 300:
            print(
                f"::warning::opened {endpoint} as {run_report_id} but finish returned HTTP {finish_status}"
            )
            continue

        published.append(
            PortalRun(
                endpoint=endpoint,
                content_root=content_root,
                passed=passed,
                failed=failed,
                skipped=skipped,
                run_report_id=str(run_report_id),
                portal_run_url=portal_run_url(
                    config.portal_url,
                    config.organization_id,
                    str(run_report_id),
                ),
            )
        )
    return published
