"""Assert the reporting step sent rows a Kerno portal could actually attribute.

The fixture is one passing scenario and one stub, both under endpoints/GET/, so the expected
result is exactly one endpoint reported with one execution — the stub ran nothing.

Reads the recorder's capture file, named by CAPTURE.
"""

import json
import os
import sys

CAPTURE = os.environ.get("CAPTURE", "/tmp/captured.jsonl")
EXPECTED_KEY = os.environ.get("EXPECTED_KEY", "self-test-key")
EXPECTED_CONTENT_ROOT = os.environ.get("EXPECTED_CONTENT_ROOT", "test-fixture")

# events-service links a test run to its run report by matching exactly these. Any drift between
# the two rows and the ledger entry silently stops resolving to the run that produced it.
IDENTITY = ("gitRepo", "gitBranch", "contentRoot", "method", "urlPath")


def main():
    if not os.path.isfile(CAPTURE):
        print(f"::error::no capture file at {CAPTURE} — the recorder never received anything")
        return 1

    with open(CAPTURE, encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]

    problems = []

    def only(suffix):
        matches = [row for row in rows if row["path"].endswith(suffix)]
        if len(matches) != 1:
            problems.append(f"expected exactly 1 POST to {suffix}, got {len(matches)}")
            return None
        return matches[0]

    test_run = only("/test-runs")
    run_report = only("/run-reports")

    if test_run and run_report:
        run_body = test_run["body"]
        report_body = run_report["body"]

        if {k: run_body.get(k) for k in IDENTITY} != {k: report_body.get(k) for k in IDENTITY}:
            problems.append("the two rows disagree on their identity fields")

        expected_repo = "github.com/{}".format(os.environ.get("GITHUB_REPOSITORY", "")).lower()
        if run_body.get("gitRepo") != expected_repo:
            problems.append(
                "gitRepo was {!r}, expected {!r} — an unqualified repo joins no endpoint row, "
                "and nothing downstream repairs it".format(run_body.get("gitRepo"), expected_repo)
            )
        if run_body.get("contentRoot") != EXPECTED_CONTENT_ROOT:
            problems.append(f"contentRoot was {run_body.get('contentRoot')!r}")

        for body, name in ((run_body, "test run"), (report_body, "run report")):
            if body.get("origin") != "github_action":
                problems.append(f"the {name} origin was {body.get('origin')!r}")
        for row, name in ((test_run, "test run"), (run_report, "run report")):
            if row["key"] != EXPECTED_KEY:
                problems.append(f"the {name} did not carry the virtual key header")

        if run_body.get("numberOfTests") != 1:
            problems.append(
                "numberOfTests was {!r}, expected 1 — the fixture's stub executed nothing and "
                "must not count as coverage".format(run_body.get("numberOfTests"))
            )
        if report_body.get("totalScenarios") != 2:
            problems.append(f"totalScenarios was {report_body.get('totalScenarios')!r}, expected 2")
        if report_body.get("diffsDetected") != 0:
            problems.append(f"diffsDetected was {report_body.get('diffsDetected')!r}, expected 0")
        if report_body.get("outcome") != "no_diffs":
            problems.append(f"outcome was {report_body.get('outcome')!r}, expected no_diffs")
        if "status" in report_body:
            problems.append("status must be omitted so the service defaults it to completed")

    for problem in problems:
        print(f"::error::{problem}")
    print(json.dumps(rows, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
