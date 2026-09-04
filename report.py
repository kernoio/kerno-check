"""Turn the replay's JUnit report into step outputs, and decide whether the step fails.

Counts come from the XML rather than from scraping the driver's stdout, so a change to its
progress output cannot silently break the outputs a workflow depends on.

Exit codes mirror the driver's, because the step's status is what gates the PR:
  0  nothing failed (scenarios may have been skipped)
  1  at least one scenario failed, and fail-on-failure is true
  2  usage or configuration error, including a missing or empty report
  3  the runner's bridge never became ready
"""

import os
import sys
import xml.etree.ElementTree as ET

EXIT_OK = 0
EXIT_SCENARIO_FAILED = 1
EXIT_USAGE = 2


def write_outputs(**values: object) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def classify(case: ET.Element) -> str:
    # `is not None` rather than truthiness: an Element with no children is falsy, so
    # `find(...) or fallback` would discard the element it just found.
    if case.find("failure") is not None:
        return "failed"
    if case.find("skipped") is not None:
        return "skipped"
    return "passed"


def main() -> int:
    junit_path = os.environ.get("KERNO_JUNIT_PATH", "")
    replay_exit = os.environ.get("KERNO_REPLAY_EXIT", "")
    fail_on_failure = os.environ.get("KERNO_FAIL_ON_FAILURE", "true").lower() == "true"

    # The driver exits 2 for a configuration error and 3 when its bridge never came up. Neither
    # produces a meaningful report, and neither should be reported as a test failure.
    if replay_exit in {"2", "3"}:
        write_outputs(total=0, passed=0, failed=0, skipped=0)
        print(f"::error::the Kerno runner could not start (exit {replay_exit}) — see the log above")
        return int(replay_exit)

    # One file for a single application; a DIRECTORY of them when `apps` replayed several, since
    # the driver writes one report per application and refuses to collapse them into one file.
    if junit_path and os.path.isdir(junit_path):
        reports = sorted(
            os.path.join(junit_path, name)
            for name in os.listdir(junit_path)
            if name.endswith(".xml")
        )
        if not reports:
            write_outputs(total=0, passed=0, failed=0, skipped=0)
            print(f"::error::no JUnit reports were written to {junit_path} — see the replay step's log")
            return EXIT_USAGE
    elif junit_path and os.path.isfile(junit_path):
        reports = [junit_path]
    else:
        write_outputs(total=0, passed=0, failed=0, skipped=0)
        print("::error::no JUnit report was produced — see the replay step's log for the cause")
        return EXIT_USAGE

    cases = []
    for report in reports:
        try:
            cases.extend(ET.parse(report).getroot().iter("testcase"))
        except ET.ParseError as error:
            write_outputs(total=0, passed=0, failed=0, skipped=0)
            print(f"::error::the JUnit report at {report} is not parseable: {error}")
            return EXIT_USAGE

    counts = {"passed": 0, "failed": 0, "skipped": 0}
    for case in cases:
        counts[classify(case)] += 1

    write_outputs(total=len(cases), **counts)

    summary = (
        f"{counts['passed']} passed, {counts['failed']} failed, "
        f"{counts['skipped']} skipped (of {len(cases)})"
    )
    print(f"Kerno check: {summary}")

    if not cases:
        # An empty report means nothing ran. Reporting that as success would make the check
        # meaningless, which is the whole failure mode this action exists to avoid.
        print("::error::the report contains no scenarios — nothing was replayed")
        return EXIT_USAGE

    if counts["failed"] > 0:
        for case in cases:
            if classify(case) != "failed":
                continue
            failure = case.find("failure")
            message = failure.get("message", "") if failure is not None else ""
            print(f"::error::{case.get('classname', '?')} — {case.get('name', '?')}: {message}")
        if fail_on_failure:
            return EXIT_SCENARIO_FAILED
        print("::warning::scenarios failed, but fail-on-failure is false")

    if counts["skipped"] > 0:
        # Stated rather than silent: skipped scenarios never executed, and a green check that
        # hides them is how a suite of stubs comes to look like coverage.
        print(
            f"::warning::{counts['skipped']} scenario(s) were skipped — blocked or not yet "
            "implemented, so they assert nothing"
        )

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
