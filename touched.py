"""Which critical endpoints a pull request puts at risk.

The check knows what a team marked (``criticality``); this is how it finds out whether the change
under review goes anywhere near it. The answer a reviewer needs is not "did the tests pass" but
"is this pull request touching something we said must not break, and did anything test it".

Changed files come from the GitHub API rather than ``git diff``. The setup this action documents is
a bare ``actions/checkout@v5``, which is ``fetch-depth: 1`` — on a pull request that is a single
merge commit, and the base commit is not in the object store at all, so a diff against it fails
outright for nearly every customer. Requiring a full clone to avoid that would tax every CI run of
every repository for a feature most runs do not use.

Not knowing is never reported as nothing. Every way this can fail to answer — a push rather than a
pull request, no token, a refused permission, an unreachable API — says so, because a false
all-clear is the only genuinely dangerous output here.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from criticality import CriticalEndpoint

# GitHub caps this at 3000 files per pull request, and a page is 100.
MAX_PAGES = 30
PER_PAGE = 100

NOT_TESTED = "not tested in this run"


@dataclass(frozen=True)
class TouchedEndpoint:
    """A critical endpoint whose file this pull request changes, and what the run said about it."""

    endpoint: CriticalEndpoint
    changed_file: str
    passed: int
    failed: int
    skipped: int

    @property
    def tested(self) -> bool:
        return bool(self.passed or self.failed or self.skipped)

    @property
    def outcome(self) -> str:
        if not self.tested:
            # The line that matters most. A critical endpoint changed with nothing run against it
            # is the exact situation this feature exists to make visible, and it must not read like
            # a pass.
            return NOT_TESTED
        if self.failed:
            return f"{self.failed} failed"
        if self.skipped and not self.passed:
            return f"{self.skipped} skipped, none executed"
        total = self.passed + self.skipped
        suffix = f", {self.skipped} skipped" if self.skipped else ""
        return f"{total} scenario{'s' if total != 1 else ''}, {self.passed} passed{suffix}"


@dataclass(frozen=True)
class TouchedReport:
    """What this run could work out about the change under review."""

    endpoints: tuple[TouchedEndpoint, ...]
    known: bool
    unknown_reason: str = ""


def _unknown(reason: str) -> TouchedReport:
    return TouchedReport(endpoints=(), known=False, unknown_reason=reason)


def pull_request_number(event_path: str) -> int | None:
    if not event_path or not os.path.isfile(event_path):
        return None
    try:
        payload = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    number = payload.get("number") or (payload.get("pull_request") or {}).get("number")
    try:
        return int(number)
    except (TypeError, ValueError):
        return None


def default_list_files(url: str, headers: dict[str, str]) -> tuple[int, str]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.getcode(), response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")


def changed_files(repo: str, number: int, token: str, *, list_files=default_list_files) -> set[str] | None:
    """Every path this pull request changes, or None when that could not be established."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "kerno-check",
    }
    paths: set[str] = set()
    for page in range(1, MAX_PAGES + 1):
        url = f"https://api.github.com/repos/{repo}/pulls/{number}/files?per_page={PER_PAGE}&page={page}"
        try:
            status, raw = list_files(url, headers)
        except (OSError, TimeoutError, ValueError):
            # Not the pages gathered so far. A partial list under-reports, and under-reporting here
            # is the false all-clear this module exists to avoid — better to say we do not know.
            return None
        if status == 403:
            # Most often a workflow without `pull-requests: read`. Not knowing, not "nothing".
            return None
        if status >= 300:
            return None
        try:
            rows = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            filename = str((row or {}).get("filename", "")).strip()
            if filename:
                paths.add(filename)
            # A rename changes two paths and only one of them is `filename`. The previous one is
            # where a critical endpoint's recorded filepath still points.
            previous = str((row or {}).get("previous_filename", "")).strip()
            if previous:
                paths.add(previous)
        if len(rows) < PER_PAGE:
            return paths
    # Every page was full and we ran out of pages. GitHub caps this API at 3000 files, so a change
    # larger than that gives a truncated list — and a truncated list under-reports, which is the one
    # answer this must never give quietly.
    return None


def _endpoint_path(endpoint: CriticalEndpoint) -> str:
    """The endpoint's file as the repository spells it, not as its module does.

    ``filepath`` is recorded relative to the module that serves the endpoint, while the API returns
    paths from the repository root. For a monorepo the two differ by the content root, and comparing
    them without joining is how a monorepo silently matches nothing.
    """
    if not endpoint.filepath:
        return ""
    if not endpoint.content_root:
        return endpoint.filepath.lstrip("./")
    root = endpoint.content_root.strip("/")
    candidate = endpoint.filepath.lstrip("./")
    if candidate.startswith(f"{root}/"):
        return candidate
    return f"{root}/{candidate}"


def touched_endpoints(
    critical: tuple[CriticalEndpoint, ...],
    changed: set[str],
    outcomes: dict[tuple[str, str], tuple[int, int, int]],
) -> tuple[TouchedEndpoint, ...]:
    """The critical endpoints whose file this change touches, with what the run said about each."""
    touched = []
    for endpoint in critical:
        path = _endpoint_path(endpoint)
        if not path or path not in changed:
            continue
        passed, failed, skipped = outcomes.get((endpoint.method, endpoint.url_path), (0, 0, 0))
        touched.append(
            TouchedEndpoint(
                endpoint=endpoint,
                changed_file=path,
                passed=passed,
                failed=failed,
                skipped=skipped,
            )
        )
    return tuple(sorted(touched, key=lambda t: (t.endpoint.url_path, t.endpoint.method)))


def report_touched(
    critical: tuple[CriticalEndpoint, ...],
    outcomes: dict[tuple[str, str], tuple[int, int, int]],
    *,
    env: dict[str, str] | None = None,
    list_files=default_list_files,
) -> TouchedReport:
    """What this run can say about critical endpoints under review, or why it cannot say it."""
    environ = env if env is not None else dict(os.environ)
    if not critical:
        # Nothing is marked, so nothing can be touched. Known, and empty.
        return TouchedReport(endpoints=(), known=True)
    if environ.get("GITHUB_EVENT_NAME", "") != "pull_request":
        return _unknown("this run is not a pull request")
    token = environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        return _unknown("no GITHUB_TOKEN was available to read the pull request's files")
    repo = environ.get("GITHUB_REPOSITORY", "").strip()
    number = pull_request_number(environ.get("GITHUB_EVENT_PATH", ""))
    if not repo or number is None:
        return _unknown("the pull request could not be identified")

    changed = changed_files(repo, number, token, list_files=list_files)
    if changed is None:
        return _unknown(
            "the pull request's files could not be read — grant `pull-requests: read` if this persists"
        )
    return TouchedReport(endpoints=touched_endpoints(critical, changed, outcomes), known=True)
