"""Optionally report a replay to a Kerno events-service.

Off unless a credential is configured: with none, this makes no request and prints nothing, which
is what keeps "no Kerno account" true for everyone who only wants the check.

It reads the JUnit report the replay already produced rather than anything Kerno-specific, so the
runner image stays a plain test runner and this logic versions with the action instead of with a
published image. The credential therefore never enters the container.

Two rows go out per endpoint. The portal's coverage figure joins `endpoints` to `test_runs` and
never reads `run_reports`, so the test run is what makes an endpoint count as covered; the run
report carries the ledger entry and its link back to this workflow run.

Always exits 0. A composite action cannot use `continue-on-error`, so nothing here may raise —
the gate is the scenario result, not our ingest.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ORIGIN = "github_action"
OUTCOME_NO_DIFFS = "no_diffs"
OUTCOME_DIFFS_REJECTED = "diffs_rejected"

REQUEST_TIMEOUT_S = 5
PHASE_BUDGET_S = 20

# `METHOD /path`, as the replay driver builds a JUnit classname from the scenario's directory.
# Anything else is the suite-name fallback it uses for a scenario outside endpoints/<METHOD>/.
ENDPOINT_RE = re.compile(r"^([A-Z]+) (/.*)$")

# The driver's own literals for a failure in our toolchain rather than in the customer's code.
# Kept exact: treating one of these as a behaviour diff would report "Issue Caught" — Kerno
# claiming it found a bug — when in fact esbuild choked or the bridge died.
INFRA_FAILURES = frozenset({"compile error", "run error", "runner error"})


def env(name):
    value = os.environ.get(name, "")
    value = value.strip()
    return value or None


def log(message):
    print(f"report: {message}")


def git_repo():
    """Host-qualified, e.g. github.com/kernoio/becore.

    Must byte-match what the Kerno agent derives from the git remote, since that is what wrote the
    endpoint rows these runs join to. Nothing downstream validates or repairs it, so a bare
    owner/repo would orphan every row silently.
    """
    repository = env("GITHUB_REPOSITORY")
    if not repository:
        return None
    server = env("GITHUB_SERVER_URL") or "https://github.com"
    host = urllib.parse.urlsplit(server).hostname
    if not host:
        return None
    path = repository.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return f"{host.lower()}/{path}" if path else None


def run_url():
    run_id = env("GITHUB_RUN_ID")
    repository = env("GITHUB_REPOSITORY")
    if not run_id or not repository:
        return None
    server = (env("GITHUB_SERVER_URL") or "https://github.com").rstrip("/")
    url = f"{server}/{repository}/actions/runs/{run_id}"
    attempt = env("GITHUB_RUN_ATTEMPT") or "1"
    return f"{url}/attempts/{attempt}" if attempt.isdigit() and int(attempt) > 1 else url


def pr_number():
    match = re.match(r"^refs/pull/(\d+)/", env("GITHUB_REF") or "")
    return int(match.group(1)) if match else None


def commit_sha():
    # On pull_request GITHUB_SHA is the ephemeral merge commit, which never exists on the remote,
    # so a link to it eventually 404s. The action passes the PR head when there is one.
    return env("KERNO_COMMIT_SHA") or env("GITHUB_SHA")


def git_branch():
    # On pull_request GITHUB_REF_NAME is `<n>/merge`, not a branch name.
    return env("GITHUB_HEAD_REF") or env("GITHUB_REF_NAME") or "unknown"


def content_root():
    """The app directory relative to the repository root, as the endpoint rows record it.

    Explicit when app-dir was given. Otherwise the replay discovered it, and since the action
    passes a single --junit file the driver refuses more than one app, so there is exactly one
    tree to find.
    """
    app_dir = env("KERNO_APP_DIR")
    if app_dir is not None:
        return app_dir.strip("/")

    workspace = env("GITHUB_WORKSPACE") or "."
    pruned = {"node_modules", ".git", "dist", "build", "coverage", ".gradle", ".idea"}
    for root, dirs, _ in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in pruned]
        if os.path.isdir(os.path.join(root, ".kerno", "scenarios")):
            dirs[:] = []
            return os.path.relpath(root, workspace).replace(os.sep, "/").lstrip("./")
    return None


def classify(case):
    failure = case.find("failure")
    if failure is not None:
        message = (failure.get("message") or "").strip()
        return "infra" if message in INFRA_FAILURES else "diff"
    if case.find("skipped") is not None:
        return "skipped"
    return "passed"


def group_by_endpoint(cases):
    """Endpoint -> outcomes. Both tables are keyed per (method, url_path), and so is the coverage join."""
    groups = {}
    unattributed = 0
    for case in cases:
        match = ENDPOINT_RE.match(case.get("classname") or "")
        if not match:
            unattributed += 1
            continue
        groups.setdefault(match.groups(), []).append(classify(case))
    return groups, unattributed


def post(base_url, org_id, virtual_key, path, body, deadline):
    """True when the row landed. Never raises.

    No retry: the test-run insert has no upsert behind it, so retrying a request that timed out
    after succeeding would duplicate the row and double-count it.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False

    request = urllib.request.Request(
        f"{base_url}/organizations/{org_id}/{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-kerno-virtual-key-id": virtual_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=min(REQUEST_TIMEOUT_S, remaining)):
            return True
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = error.read(200).decode("utf-8", "replace")
        except Exception:
            pass
        # The URL carries the organization id and the key rides in a header; neither is logged.
        log(f"{path} returned {error.code} {detail}".rstrip())
        return False
    except Exception as error:
        log(f"{path} failed: {error}")
        return False


def identity(method, url_path, root, repo, branch):
    """events-service links a test run to its run report by matching exactly these fields."""
    return {
        "gitRepo": repo,
        "gitBranch": branch,
        "contentRoot": root,
        "method": method,
        "urlPath": url_path,
    }


def main():
    org_id = env("KERNO_ORGANIZATION_ID")
    virtual_key = env("KERNO_VIRTUAL_KEY_ID")
    if not org_id and not virtual_key:
        return 0
    if not org_id or not virtual_key:
        log("skipped — kerno-org and kerno-virtual-key must both be set")
        return 0

    base_url = env("KERNO_EVENTS_URL")
    if not base_url or urllib.parse.urlsplit(base_url).scheme not in {"http", "https"}:
        log("skipped — kerno-url must be an http or https URL")
        return 0
    base_url = base_url.rstrip("/")

    repo = git_repo()
    sha = commit_sha()
    if not repo or not sha:
        log("skipped — GITHUB_REPOSITORY and GITHUB_SHA are required to attribute a run")
        return 0

    root = content_root()
    if root is None:
        log("skipped — could not determine the app directory; set app-dir")
        return 0

    junit_path = env("KERNO_JUNIT_PATH")
    if not junit_path or not os.path.isfile(junit_path):
        log("skipped — no JUnit report to read")
        return 0
    try:
        cases = list(ET.parse(junit_path).getroot().iter("testcase"))
    except ET.ParseError as error:
        log(f"skipped — the JUnit report is not parseable: {error}")
        return 0

    groups, unattributed = group_by_endpoint(cases)
    if unattributed:
        log(f"{unattributed} scenario(s) outside endpoints/<METHOD>/ not attributable")

    branch = git_branch()
    pr = pr_number()
    url = run_url()

    reported = 0
    planned = []
    inert = 0
    for (method, url_path), outcomes in groups.items():
        # A scenario that never ran, or failed to compile, did not exercise the endpoint. Coverage
        # is a bare existence check that never reads numberOfTests, so a row claiming zero tests
        # would still mark the endpoint fully covered.
        executed = sum(1 for o in outcomes if o in ("passed", "diff"))
        if executed == 0:
            inert += 1
            continue
        planned.append((method, url_path, outcomes, executed))

    if inert:
        log(f"{inert} of {len(groups)} endpoint(s) executed nothing, not reported")
    if not planned:
        return 0

    deadline = time.monotonic() + PHASE_BUDGET_S
    for method, url_path, outcomes, executed in sorted(planned):
        if time.monotonic() >= deadline:
            break
        shared = identity(method, url_path, root, repo, branch)
        diffs = sum(1 for o in outcomes if o == "diff")

        test_run = dict(
            shared,
            filepath=f".kerno/scenarios/endpoints/{method}{url_path.rstrip('/')}",
            # A replay has no source range; zero is the honest unknown.
            startLine=0,
            endLine=0,
            numberOfTests=executed,
            commitSha=sha,
            origin=ORIGIN,
            # mode and effort are the strategy and effort a run was planned under; a replay
            # inherits neither.
        )
        run_report = dict(
            shared,
            commitSha=sha,
            origin=ORIGIN,
            # `status` omitted so it defaults to completed: a replay is over by the time we
            # report, and the finish request cannot carry origin, prNumber or runUrl anyway.
            outcome=OUTCOME_DIFFS_REJECTED if diffs else OUTCOME_NO_DIFFS,
            totalScenarios=len(outcomes),
            diffsDetected=diffs,
            # A replay executes a committed suite and changes nothing about it.
            scenariosAdded=0,
            scenariosUpdated=0,
            scenariosRemoved=0,
        )
        if pr is not None:
            run_report["prNumber"] = pr
        if url is not None:
            run_report["runUrl"] = url

        # Test run first: if the budget expires mid-endpoint, lose the ledger row, not the
        # coverage row.
        ok = post(base_url, org_id, virtual_key, "test-runs", test_run, deadline)
        ok = post(base_url, org_id, virtual_key, "run-reports", run_report, deadline) and ok
        if ok:
            reported += 1

    if reported < len(planned):
        log(f"{reported} of {len(planned)} endpoint(s) reported as {ORIGIN}")
    else:
        log(f"{reported} endpoint(s) reported as {ORIGIN}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        # The scenario results are already written and the step's gate is a separate script.
        log(f"aborted: {error}")
        sys.exit(0)
