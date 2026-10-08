"""Send the default branch's committed Kerno scenarios to events-service after a merge.

Runs in `mode: sync`. Nothing is replayed: the scenario files are read, one snapshot is sent, and the
step summary says what the merge changed on the default branch. Reporting never fails the step.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Any

from portal import DEFAULT_EVENTS_URL, HttpCall, default_http_put, json_headers

EXIT_OK = 0
EXIT_USAGE = 2

KERNO_DIR = ".kerno"
SCENARIO_SUFFIX = ".scenario.ts"
PLAN_FILE = "plan.json"
SKIPPED_DIRS = {".git", "node_modules"}
MAX_SCENARIOS = 20000
MAX_LISTED = 20

META = re.compile(r"export\s+const\s+meta\b")
META_PATH = re.compile(r"""\bpath\s*:\s*(['"`])\s*([A-Za-z]+)\s+(\S[^'"`]*?)\s*\1""")


@dataclass(frozen=True)
class SyncConfig:
    api_key: str
    organization_id: str
    events_url: str


@dataclass(frozen=True)
class Discovery:
    endpoints: list[dict[str, Any]]
    unreadable: list[str]
    duplicates: list[str]

    @property
    def scenario_count(self) -> int:
        return sum(len(endpoint["scenarios"]) for endpoint in self.endpoints)


def load_sync_config() -> SyncConfig | None:
    api_key = os.environ.get("KERNO_API_KEY", "").strip()
    organization_id = os.environ.get("KERNO_ORGANIZATION_ID", "").strip()
    if not api_key and not organization_id:
        return None
    if not api_key or not organization_id:
        raise ValueError("api-key and organization-id must be set together — one without the other cannot sync")
    return SyncConfig(
        api_key=api_key,
        organization_id=organization_id,
        events_url=os.environ.get("KERNO_EVENTS_URL", "").strip() or DEFAULT_EVENTS_URL,
    )


def default_branch(event_path: str) -> str | None:
    if not event_path or not os.path.isfile(event_path):
        return None
    try:
        with open(event_path, encoding="utf-8") as handle:
            event = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    repository = event.get("repository") if isinstance(event, dict) else None
    branch = repository.get("default_branch") if isinstance(repository, dict) else None
    return branch if isinstance(branch, str) and branch else None


def parse_meta_path(source: str) -> tuple[str, str] | None:
    meta = META.search(source)
    if meta is None:
        return None
    match = META_PATH.search(source, meta.end())
    if match is None:
        return None
    return match.group(2).upper(), match.group(3).strip()


def load_plan(directory: str) -> dict[str, tuple[str | None, str | None]]:
    try:
        with open(os.path.join(directory, PLAN_FILE), encoding="utf-8") as handle:
            plan = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    entries = plan.get("scenarios") if isinstance(plan, dict) else None
    if not isinstance(entries, list):
        return {}
    details: dict[str, tuple[str | None, str | None]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        spec = entry.get("spec") if isinstance(entry.get("spec"), dict) else {}
        title = spec.get("title")
        kind = spec.get("kind")
        details[entry["id"]] = (
            title if isinstance(title, str) and title.strip() else None,
            kind if isinstance(kind, str) and kind.strip() else None,
        )
    return details


def discover(workspace: str) -> Discovery:
    grouped: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = {}
    unreadable: list[str] = []
    duplicates: list[str] = []

    for root, dirs, _ in os.walk(workspace):
        dirs[:] = sorted(d for d in dirs if d not in SKIPPED_DIRS)
        if os.path.basename(root) != KERNO_DIR:
            continue
        dirs[:] = []
        content_root = os.path.relpath(os.path.dirname(root), workspace).replace(os.sep, "/")
        content_root = "" if content_root == "." else content_root
        endpoints_dir = os.path.join(root, "scenarios", "endpoints")

        for scenario_root, scenario_dirs, files in os.walk(endpoints_dir):
            scenario_dirs.sort()
            plan = None
            for name in sorted(files):
                if not name.endswith(SCENARIO_SUFFIX):
                    continue
                path = os.path.join(scenario_root, name)
                file_path = os.path.relpath(path, workspace).replace(os.sep, "/")
                try:
                    with open(path, encoding="utf-8") as handle:
                        endpoint = parse_meta_path(handle.read())
                except (OSError, UnicodeDecodeError):
                    endpoint = None
                if endpoint is None:
                    unreadable.append(file_path)
                    continue

                if plan is None:
                    plan = load_plan(scenario_root)
                scenario_id = name[: -len(SCENARIO_SUFFIX)]
                scenarios = grouped.setdefault((content_root, *endpoint), {})
                if scenario_id in scenarios:
                    duplicates.append(file_path)
                    continue
                title, kind = plan.get(scenario_id, (None, None))
                scenarios[scenario_id] = {"id": scenario_id, "filePath": file_path, "title": title, "kind": kind}

    endpoints = [
        {
            "contentRoot": content_root,
            "method": method,
            "urlPath": url_path,
            "scenarios": [scenarios[key] for key in sorted(scenarios)],
        }
        for (content_root, method, url_path), scenarios in sorted(grouped.items())
    ]
    return Discovery(endpoints=endpoints, unreadable=unreadable, duplicates=duplicates)


def run_url() -> str | None:
    server = os.environ.get("GITHUB_SERVER_URL", "").strip()
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    if not server or not repository or not run_id:
        return None
    return f"{server.rstrip('/')}/{repository}/actions/runs/{run_id}"


def build_snapshot(branch: str, discovery: Discovery) -> dict[str, Any]:
    return {
        "gitRepo": os.environ.get("GITHUB_REPOSITORY", "").strip(),
        "gitBranch": branch,
        "commitSha": os.environ.get("GITHUB_SHA", "").strip(),
        "runUrl": run_url(),
        "endpoints": discovery.endpoints,
    }


def send(snapshot: dict[str, Any], config: SyncConfig, http_put: HttpCall) -> dict[str, Any] | None:
    url = f"{config.events_url.rstrip('/')}/organizations/{config.organization_id}/repo-snapshots"
    try:
        status, raw = http_put(url, json_headers(config.api_key), json.dumps(snapshot).encode("utf-8"))
    except (OSError, TimeoutError) as error:
        print(f"::warning::could not sync the Kerno snapshot: {error}")
        return None
    if status >= 300:
        print(f"::warning::could not sync the Kerno snapshot (HTTP {status})")
        return None
    try:
        response = json.loads(raw)
    except json.JSONDecodeError:
        print("::warning::events-service accepted the Kerno snapshot but returned no readable summary")
        return None
    return response if isinstance(response, dict) else None


def _short(sha: str) -> str:
    return sha[:7]


def _blob(repository: str, sha: str, file_path: str) -> str:
    server = os.environ.get("GITHUB_SERVER_URL", "").strip() or "https://github.com"
    return f"{server.rstrip('/')}/{repository}/blob/{sha}/{file_path}"


def _scenario_link(scenario: dict[str, Any], repository: str, sha: str) -> str:
    label = scenario.get("title") or scenario.get("id") or "?"
    file_path = scenario.get("filePath")
    return f"[{label}]({_blob(repository, sha, file_path)})" if file_path and sha else str(label)


def _endpoint_label(endpoint: dict[str, Any]) -> str:
    label = f"`{endpoint.get('method', '?')} {endpoint.get('urlPath', '?')}`"
    content_root = endpoint.get("contentRoot")
    return f"{label} ({content_root})" if content_root else label


def _bounded(lines: list[str]) -> list[str]:
    if len(lines) <= MAX_LISTED:
        return lines
    return lines[:MAX_LISTED] + [f"- …and {len(lines) - MAX_LISTED} more"]


def _signed(value: int) -> str:
    return f"+{value}" if value >= 0 else str(value)


def format_summary(response: dict[str, Any], repository: str) -> str:
    snapshot = response.get("snapshot") or {}
    previous = response.get("previous")
    changes = response.get("changes") or {}
    sha = str(snapshot.get("commitSha") or "")
    endpoints = snapshot.get("endpoints") or []
    endpoint_count = len(endpoints)
    scenario_count = sum(len(endpoint.get("scenarios") or []) for endpoint in endpoints)

    lines = [
        f"### Kerno · {snapshot.get('gitBranch', '?')} @ `{_short(sha)}`",
        "",
    ]
    totals = f"**{endpoint_count}** endpoints · **{scenario_count}** scenarios"
    if not previous:
        lines.append(f"First snapshot of {snapshot.get('gitBranch', 'the default branch')}: {totals}.")
        return "\n".join(lines) + "\n"

    previous_sha = str(previous.get("commitSha") or "")
    endpoint_delta = endpoint_count - int(previous.get("endpointCount") or 0)
    scenario_delta = scenario_count - int(previous.get("scenarioCount") or 0)
    lines.append(
        f"{totals} — {_signed(endpoint_delta)} endpoints, {_signed(scenario_delta)} scenarios "
        f"since `{_short(previous_sha)}`."
    )

    added = changes.get("added") or []
    removed = changes.get("removed") or []
    changed = changes.get("changed") or []
    if not added and not removed and not changed:
        lines += ["", "No scenario changes."]
        return "\n".join(lines) + "\n"

    if added:
        lines += ["", "#### Added endpoints"]
        lines += _bounded([
            f"- {_endpoint_label(endpoint)}: "
            + ", ".join(_scenario_link(s, repository, sha) for s in endpoint.get("scenarios") or [])
            for endpoint in added
        ])
    if removed:
        lines += ["", "#### Removed endpoints"]
        lines += _bounded([
            f"- {_endpoint_label(endpoint)}: "
            + ", ".join(_scenario_link(s, repository, previous_sha) for s in endpoint.get("scenarios") or [])
            for endpoint in removed
        ])
    if changed:
        lines += ["", "#### Changed endpoints"]
        rows = []
        for endpoint in changed:
            parts = [f"+{_scenario_link(s, repository, sha)}" for s in endpoint.get("addedScenarios") or []]
            parts += [f"−{_scenario_link(s, repository, previous_sha)}" for s in endpoint.get("removedScenarios") or []]
            rows.append(
                f"- {_endpoint_label(endpoint)} "
                f"({endpoint.get('fromScenarioCount', '?')} → {endpoint.get('toScenarioCount', '?')}): "
                + ", ".join(parts)
            )
        lines += _bounded(rows)
    return "\n".join(lines) + "\n"


def write_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
    except OSError as error:
        print(f"Kerno sync: could not write the step summary ({error})")


def main(http_put: HttpCall = default_http_put) -> int:
    try:
        config = load_sync_config()
    except ValueError as error:
        print(f"::error::{error}")
        return EXIT_USAGE
    if config is None:
        print("Kerno sync: no api-key, so nothing is sent")
        return EXIT_OK

    branch = os.environ.get("GITHUB_REF_NAME", "").strip()
    expected = default_branch(os.environ.get("GITHUB_EVENT_PATH", "").strip())
    if not os.environ.get("GITHUB_REF", "").startswith("refs/heads/") or not branch:
        print("::notice::Kerno sync runs on a branch push; this run is not one, so nothing is sent")
        return EXIT_OK
    if expected is None:
        print("::notice::Kerno sync could not tell the repository's default branch, so nothing is sent")
        return EXIT_OK
    if branch != expected:
        print(f"::notice::Kerno sync only records {expected}; this run is on {branch}, so nothing is sent")
        return EXIT_OK
    if not os.environ.get("GITHUB_SHA", "").strip() or not os.environ.get("GITHUB_REPOSITORY", "").strip():
        print("::warning::Kerno sync needs GITHUB_SHA and GITHUB_REPOSITORY; nothing is sent")
        return EXIT_OK

    discovery = discover(os.environ.get("GITHUB_WORKSPACE", "").strip() or ".")
    if discovery.unreadable:
        print(
            f"::warning::{len(discovery.unreadable)} scenario file(s) have no readable meta.path and were "
            f"left out: {', '.join(discovery.unreadable[:5])}"
        )
    if discovery.duplicates:
        print(
            f"::warning::{len(discovery.duplicates)} scenario file(s) repeat an id already found for the same "
            f"endpoint and were left out: {', '.join(discovery.duplicates[:5])}"
        )
    if discovery.scenario_count > MAX_SCENARIOS:
        print(f"::warning::{discovery.scenario_count} scenarios exceed the {MAX_SCENARIOS} a snapshot may carry; nothing is sent")
        return EXIT_OK

    print(
        f"Kerno sync: {len(discovery.endpoints)} endpoints, {discovery.scenario_count} scenarios "
        f"on {branch} @ {_short(os.environ['GITHUB_SHA'].strip())}"
    )
    response = send(build_snapshot(branch, discovery), config, http_put)
    if response is None:
        return EXIT_OK

    summary = format_summary(response, os.environ["GITHUB_REPOSITORY"].strip())
    print(summary)
    write_summary(summary)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
