"""Open one portal run report per endpoint after replay, and return the URLs.

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
from xml.etree.ElementTree import Element

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
class ReplayCase:
    content_root: str
    element: Element


@dataclass(frozen=True)
class PortalRun:
    endpoint: str
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


def parse_endpoint(classname: str) -> tuple[str, str] | None:
    label = classname.strip()
    if not label:
        return None
    method, _, path = label.partition(" ")
    if not method:
        return None
    return method, (path.strip() or "/")


def group_by_endpoint(cases: list[ReplayCase]) -> dict[tuple[str, str, str], list[Element]]:
    grouped: dict[tuple[str, str, str], list[Element]] = defaultdict(list)
    for case in cases:
        parsed = parse_endpoint(case.element.get("classname", ""))
        if parsed is None:
            continue
        method, path = parsed
        grouped[(case.content_root, method, path)].append(case.element)
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


def _case_status(case: Element) -> str:
    if case.find("failure") is not None:
        return "failed"
    if case.find("skipped") is not None:
        return "skipped"
    return "passed"


def _scenario_row(case: Element) -> dict[str, Any]:
    status = _case_status(case)
    name = case.get("name") or "unknown"
    if status == "failed":
        return {
            "scenarioId": name,
            "state": "diff_detected",
            "scenario": name,
            "verdict": "failed",
            "hasErrors": True,
            "responseStatus": 0,
        }
    if status == "skipped":
        return {
            "scenarioId": name,
            "state": "not_implemented",
            "scenario": name,
            "verdict": "not_implemented",
            "hasErrors": False,
            "responseStatus": 0,
        }
    return {
        "scenarioId": name,
        "state": "unchanged",
        "scenario": name,
        "verdict": "passed",
        "hasErrors": False,
        "responseStatus": 0,
    }


def publish_runs(
    cases: list[ReplayCase],
    config: PortalConfig,
    *,
    http_post: HttpCall = default_http,
    http_patch: HttpCall = default_http_patch,
) -> list[PortalRun]:
    events_base = config.events_url.rstrip("/")
    published: list[PortalRun] = []
    for (content_root, method, path), group in sorted(group_by_endpoint(cases).items()):
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

        failed = sum(1 for case in group if _case_status(case) == "failed")
        finish_body = json.dumps(
            {
                "status": "completed",
                "outcome": "diffs_rejected" if failed else "no_diffs",
                "totalScenarios": len(group),
                "diffsDetected": failed,
                "scenariosAdded": 0,
                "scenariosUpdated": 0,
                "scenariosRemoved": 0,
                "report": {"scenarios": [_scenario_row(case) for case in group]},
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
                run_report_id=str(run_report_id),
                portal_run_url=portal_run_url(
                    config.portal_url,
                    config.organization_id,
                    str(run_report_id),
                ),
            )
        )
    return published
