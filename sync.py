"""`mode: sync` — record the default branch's committed scenarios after a merge.

1. Decide whether this run records anything (credentials, default branch).
2. Read every committed scenario.
3. Send them to events-service as this commit's snapshot.
4. Write what the merge changed to the job summary.

Nothing is replayed, and a failure to send never fails the step.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Any

from portal import Credentials, HttpCall, default_http_put, json_headers, load_credentials

EXIT_OK = 0
EXIT_USAGE = 2

KERNO_DIR = ".kerno"
SCENARIO_SUFFIX = ".scenario.ts"
PLAN_FILE = "plan.json"
SKIPPED_DIRS = {".git", "node_modules"}
MAX_SCENARIOS = 20000
MAX_LISTED = 20
MAX_NAMED_FILES = 5

META = re.compile(r"export\s+const\s+meta\b")
META_PATH = re.compile(r"""\bpath\s*:\s*(['"`])\s*([A-Za-z]+)\s+(\S[^'"`]*?)\s*\1""")


# 1. Does this run record anything?


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


def branch_to_record() -> str | None:
    branch = os.environ.get("GITHUB_REF_NAME", "").strip()
    if not os.environ.get("GITHUB_REF", "").startswith("refs/heads/") or not branch:
        print("::notice::Kerno sync runs on a branch push; this run is not one, so nothing is sent")
        return None

    expected = default_branch(os.environ.get("GITHUB_EVENT_PATH", "").strip())
    if expected is None:
        print("::notice::Kerno sync could not tell the repository's default branch, so nothing is sent")
        return None
    if branch != expected:
        print(f"::notice::Kerno sync only records {expected}; this run is on {branch}, so nothing is sent")
        return None

    if not os.environ.get("GITHUB_SHA", "").strip() or not os.environ.get("GITHUB_REPOSITORY", "").strip():
        print("::warning::Kerno sync needs GITHUB_SHA and GITHUB_REPOSITORY; nothing is sent")
        return None
    return branch


# 2. Read the committed scenarios.


@dataclass(frozen=True)
class Discovery:
    endpoints: list[dict[str, Any]]
    unreadable: list[str]
    duplicates: list[str]

    @property
    def scenario_count(self) -> int:
        return sum(len(endpoint["scenarios"]) for endpoint in self.endpoints)


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
        details[entry["id"]] = (_text_or_none(spec.get("title")), _text_or_none(spec.get("kind")))
    return details


def discover(workspace: str) -> Discovery:
    grouped: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = {}
    unreadable: list[str] = []
    duplicates: list[str] = []

    for kerno_dir in _kerno_dirs(workspace):
        content_root = _relative(os.path.dirname(kerno_dir), workspace)
        for directory, files in _scenario_dirs(kerno_dir):
            plan = load_plan(directory)
            for name in files:
                file_path = _relative(os.path.join(directory, name), workspace)
                endpoint = _read_endpoint(os.path.join(directory, name))
                if endpoint is None:
                    unreadable.append(file_path)
                    continue

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
            "scenarios": [scenarios[scenario_id] for scenario_id in sorted(scenarios)],
        }
        for (content_root, method, url_path), scenarios in sorted(grouped.items())
    ]
    return Discovery(endpoints=endpoints, unreadable=unreadable, duplicates=duplicates)


def _kerno_dirs(workspace: str) -> list[str]:
    found = []
    for root, dirs, _ in os.walk(workspace):
        dirs[:] = sorted(d for d in dirs if d not in SKIPPED_DIRS)
        if os.path.basename(root) == KERNO_DIR:
            found.append(root)
            dirs[:] = []
    return found


def _scenario_dirs(kerno_dir: str) -> list[tuple[str, list[str]]]:
    found = []
    for directory, dirs, files in os.walk(os.path.join(kerno_dir, "scenarios", "endpoints")):
        dirs.sort()
        scenario_files = sorted(name for name in files if name.endswith(SCENARIO_SUFFIX))
        if scenario_files:
            found.append((directory, scenario_files))
    return found


def _read_endpoint(path: str) -> tuple[str, str] | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return parse_meta_path(handle.read())
    except (OSError, UnicodeDecodeError):
        return None


def _relative(path: str, workspace: str) -> str:
    relative = os.path.relpath(path, workspace).replace(os.sep, "/")
    return "" if relative == "." else relative


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def report_left_out(discovery: Discovery) -> None:
    if discovery.unreadable:
        print(
            f"::warning::{len(discovery.unreadable)} scenario file(s) have no readable meta.path and were "
            f"left out: {', '.join(discovery.unreadable[:MAX_NAMED_FILES])}"
        )
    if discovery.duplicates:
        print(
            f"::warning::{len(discovery.duplicates)} scenario file(s) repeat an id already found for the same "
            f"endpoint and were left out: {', '.join(discovery.duplicates[:MAX_NAMED_FILES])}"
        )


# 3. Send the snapshot.


def build_snapshot(branch: str, discovery: Discovery) -> dict[str, Any]:
    return {
        "gitRepo": os.environ["GITHUB_REPOSITORY"].strip(),
        "gitBranch": branch,
        "commitSha": os.environ["GITHUB_SHA"].strip(),
        "runUrl": _run_url(),
        "endpoints": discovery.endpoints,
    }


def send(snapshot: dict[str, Any], credentials: Credentials, http_put: HttpCall) -> dict[str, Any] | None:
    url = f"{credentials.events_url.rstrip('/')}/organizations/{credentials.organization_id}/repo-snapshots"
    try:
        status, raw = http_put(
            url, json_headers(credentials.virtual_key_id), json.dumps(snapshot).encode("utf-8")
        )
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


def _run_url() -> str | None:
    server = os.environ.get("GITHUB_SERVER_URL", "").strip()
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    if not server or not repository or not run_id:
        return None
    return f"{server.rstrip('/')}/{repository}/actions/runs/{run_id}"


# 4. Summarise what the merge changed.


def format_summary(response: dict[str, Any], repository: str) -> str:
    snapshot = response.get("snapshot") or {}
    previous = response.get("previous")
    changes = response.get("changes") or {}
    branch = snapshot.get("gitBranch") or "the default branch"
    sha = str(snapshot.get("commitSha") or "")
    endpoints = snapshot.get("endpoints") or []
    endpoint_count = len(endpoints)
    scenario_count = sum(len(endpoint.get("scenarios") or []) for endpoint in endpoints)
    totals = f"**{endpoint_count}** endpoints · **{scenario_count}** scenarios"

    lines = [f"### Kerno · {branch} @ `{_short(sha)}`", ""]
    if not previous:
        lines.append(f"First snapshot of {branch}: {totals}.")
        return _joined(lines)

    previous_sha = str(previous.get("commitSha") or "")
    endpoint_delta = endpoint_count - int(previous.get("endpointCount") or 0)
    scenario_delta = scenario_count - int(previous.get("scenarioCount") or 0)
    lines.append(
        f"{totals} — {_signed(endpoint_delta)} endpoints, {_signed(scenario_delta)} scenarios "
        f"since `{_short(previous_sha)}`."
    )

    links = _Links(repository=repository, current_sha=sha, previous_sha=previous_sha)
    sections = [
        _section("Added endpoints", [_endpoint_row(e, links.current) for e in changes.get("added") or []]),
        _section("Removed endpoints", [_endpoint_row(e, links.previous) for e in changes.get("removed") or []]),
        _section("Changed endpoints", [_changed_row(e, links) for e in changes.get("changed") or []]),
    ]
    sections = [section for section in sections if section]
    if not sections:
        lines += ["", "No scenario changes."]
    for section in sections:
        lines += ["", *section]
    return _joined(lines)


@dataclass(frozen=True)
class _Links:
    repository: str
    current_sha: str
    previous_sha: str

    def current(self, scenario: dict[str, Any]) -> str:
        return self._link(scenario, self.current_sha)

    def previous(self, scenario: dict[str, Any]) -> str:
        return self._link(scenario, self.previous_sha)

    def _link(self, scenario: dict[str, Any], sha: str) -> str:
        label = scenario.get("title") or scenario.get("id") or "?"
        file_path = scenario.get("filePath")
        if not file_path or not sha:
            return str(label)
        server = os.environ.get("GITHUB_SERVER_URL", "").strip() or "https://github.com"
        return f"[{label}]({server.rstrip('/')}/{self.repository}/blob/{sha}/{file_path})"


def _endpoint_row(endpoint: dict[str, Any], link) -> str:
    scenarios = ", ".join(link(scenario) for scenario in endpoint.get("scenarios") or [])
    return f"- {_endpoint_label(endpoint)}: {scenarios}"


def _changed_row(endpoint: dict[str, Any], links: _Links) -> str:
    added = [f"+{links.current(scenario)}" for scenario in endpoint.get("addedScenarios") or []]
    removed = [f"−{links.previous(scenario)}" for scenario in endpoint.get("removedScenarios") or []]
    counts = f"{endpoint.get('fromScenarioCount', '?')} → {endpoint.get('toScenarioCount', '?')}"
    return f"- {_endpoint_label(endpoint)} ({counts}): {', '.join(added + removed)}"


def _section(title: str, rows: list[str]) -> list[str]:
    if not rows:
        return []
    if len(rows) > MAX_LISTED:
        rows = rows[:MAX_LISTED] + [f"- …and {len(rows) - MAX_LISTED} more"]
    return [f"#### {title}", *rows]


def _endpoint_label(endpoint: dict[str, Any]) -> str:
    label = f"`{endpoint.get('method', '?')} {endpoint.get('urlPath', '?')}`"
    content_root = endpoint.get("contentRoot")
    return f"{label} ({content_root})" if content_root else label


def _short(sha: str) -> str:
    return sha[:7]


def _signed(value: int) -> str:
    return f"+{value}" if value >= 0 else str(value)


def _joined(lines: list[str]) -> str:
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


def main(http_put: HttpCall = default_http_put, http_post: HttpCall | None = None) -> int:
    branch = branch_to_record()
    if branch is None:
        return EXIT_OK

    try:
        credentials = load_credentials(http_post)
    except ValueError as error:
        print(f"::error::{error}")
        return EXIT_USAGE
    if credentials is None:
        return EXIT_OK

    discovery = discover(os.environ.get("GITHUB_WORKSPACE", "").strip() or ".")
    report_left_out(discovery)
    if discovery.scenario_count > MAX_SCENARIOS:
        print(
            f"::warning::{discovery.scenario_count} scenarios exceed the {MAX_SCENARIOS} a snapshot may carry; "
            "nothing is sent"
        )
        return EXIT_OK

    snapshot = build_snapshot(branch, discovery)
    print(
        f"Kerno sync: {len(discovery.endpoints)} endpoints, {discovery.scenario_count} scenarios "
        f"on {branch} @ {_short(snapshot['commitSha'])}"
    )
    response = send(snapshot, credentials, http_put)
    if response is None:
        return EXIT_OK

    summary = format_summary(response, snapshot["gitRepo"])
    print(summary)
    write_summary(summary)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
