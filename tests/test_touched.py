from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from criticality import CriticalEndpoint  # noqa: E402
from touched import (  # noqa: E402
    NOT_TESTED,
    changed_files,
    report_touched,
    touched_endpoints,
)


def critical(method="POST", url_path="/checkout", content_root="", filepath="src/checkout.ts"):
    return CriticalEndpoint(content_root, method, url_path, filepath)


def pr_env(tmp_event: str) -> dict[str, str]:
    return {
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_TOKEN": "gh-token",
        "GITHUB_REPOSITORY": "kernoio/becore",
        "GITHUB_EVENT_PATH": tmp_event,
    }


def event_file(tmp_path: str, number: int = 7) -> str:
    path = os.path.join(tmp_path, "event.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"pull_request": {"number": number}}, handle)
    return path


# `"page=1" in url` would also match `per_page=100`, which silently makes every page look like
# page one. Matched on the parameter itself instead.
def page_of(url: str) -> str:
    return dict(pair.split("=", 1) for pair in url.split("?", 1)[1].split("&")).get("page", "")


def files_page(*names: str):
    def list_files(url, headers):
        if page_of(url) == "1":
            return 200, json.dumps([{"filename": n} for n in names])
        return 200, "[]"

    return list_files


class TouchedEndpointsTest(unittest.TestCase):
    def test_an_endpoint_whose_file_changed_is_touched(self) -> None:
        touched = touched_endpoints((critical(),), {"src/checkout.ts"}, {("POST", "/checkout"): (3, 0, 0)})

        self.assertEqual(len(touched), 1)
        self.assertEqual(touched[0].changed_file, "src/checkout.ts")
        self.assertIn("3 passed", touched[0].outcome)

    def test_an_endpoint_whose_file_did_not_change_is_not_touched(self) -> None:
        self.assertEqual(touched_endpoints((critical(),), {"README.md"}, {}), ())

    def test_a_touched_endpoint_nothing_ran_against_says_so(self) -> None:
        touched = touched_endpoints((critical(),), {"src/checkout.ts"}, {})

        # The line that matters most: changed, critical, and untested. It must not read like a pass.
        self.assertEqual(touched[0].outcome, NOT_TESTED)

    def test_a_failure_is_reported_ahead_of_the_passes(self) -> None:
        touched = touched_endpoints((critical(),), {"src/checkout.ts"}, {("POST", "/checkout"): (2, 1, 0)})

        self.assertEqual(touched[0].outcome, "1 failed")

    def test_a_module_s_filepath_is_joined_to_its_content_root(self) -> None:
        endpoint = critical(content_root="apps/api", filepath="src/users.ts")

        # filepath is relative to the module; the API returns repository-root paths. Comparing them
        # unjoined is how a monorepo silently matches nothing.
        self.assertEqual(touched_endpoints((endpoint,), {"apps/api/src/users.ts"}, {})[0].changed_file,
                         "apps/api/src/users.ts")
        self.assertEqual(touched_endpoints((endpoint,), {"src/users.ts"}, {}), ())

    def test_a_filepath_already_carrying_its_content_root_is_not_doubled(self) -> None:
        endpoint = critical(content_root="apps/api", filepath="apps/api/src/users.ts")

        self.assertEqual(len(touched_endpoints((endpoint,), {"apps/api/src/users.ts"}, {})), 1)

    def test_an_endpoint_with_no_known_location_cannot_be_matched(self) -> None:
        # Kept in the set by BE-3195 on purpose, but there is nothing here to intersect it with.
        self.assertEqual(touched_endpoints((critical(filepath=""),), {"src/checkout.ts"}, {}), ())


class ChangedFilesTest(unittest.TestCase):
    def test_reads_every_page(self) -> None:
        def list_files(url, headers):
            if page_of(url) == "1":
                return 200, json.dumps([{"filename": f"f{i}.ts"} for i in range(100)])
            if page_of(url) == "2":
                return 200, json.dumps([{"filename": "last.ts"}])
            return 200, "[]"

        found = changed_files("kernoio/becore", 7, "t", list_files=list_files)

        self.assertEqual(len(found), 101)
        self.assertIn("last.ts", found)

    def test_a_rename_contributes_both_of_its_paths(self) -> None:
        def list_files(url, headers):
            if page_of(url) == "1":
                return 200, json.dumps([{"filename": "new.ts", "previous_filename": "old.ts"}])
            return 200, "[]"

        # A critical endpoint's recorded filepath still points at the old name, so a rename that
        # only contributed the new one would miss exactly the endpoint the rename disturbed.
        self.assertEqual(changed_files("r", 1, "t", list_files=list_files), {"new.ts", "old.ts"})

    def test_a_refused_permission_is_not_an_empty_change_set(self) -> None:
        self.assertIsNone(changed_files("r", 1, "t", list_files=lambda *_: (403, "no")))

    def test_a_failure_part_way_through_discards_the_partial_answer(self) -> None:
        def list_files(url, headers):
            if page_of(url) == "1":
                return 200, json.dumps([{"filename": f"f{i}.ts"} for i in range(100)])
            raise OSError("connection reset")

        # A partial list under-reports, and under-reporting is the false all-clear this avoids.
        self.assertIsNone(changed_files("r", 1, "t", list_files=list_files))

    def test_a_change_too_large_for_the_api_is_not_a_partial_answer(self) -> None:
        def list_files(url, headers):
            return 200, json.dumps([{"filename": f"f{page_of(url)}-{i}.ts"} for i in range(100)])

        # GitHub caps this API at 3000 files. Past that the list is truncated, and a truncated list
        # under-reports — so it answers "do not know" rather than a subset that looks complete.
        self.assertIsNone(changed_files("r", 1, "t", list_files=list_files))

    def test_an_unreadable_body_is_not_an_empty_change_set(self) -> None:
        self.assertIsNone(changed_files("r", 1, "t", list_files=lambda *_: (200, "<html>")))


class ReportTouchedTest(unittest.TestCase):
    def test_nothing_marked_is_known_and_empty(self) -> None:
        report = report_touched((), {}, env={})

        # Nothing is critical, so nothing can be touched. That is an answer, not a failure.
        self.assertTrue(report.known)
        self.assertEqual(report.endpoints, ())

    def test_a_push_build_says_it_cannot_tell(self) -> None:
        report = report_touched((critical(),), {}, env={"GITHUB_EVENT_NAME": "push"})

        self.assertFalse(report.known)
        self.assertIn("not a pull request", report.unknown_reason)

    def test_no_token_says_it_cannot_tell(self) -> None:
        report = report_touched((critical(),), {}, env={"GITHUB_EVENT_NAME": "pull_request"})

        self.assertFalse(report.known)
        self.assertIn("GITHUB_TOKEN", report.unknown_reason)

    def test_a_refused_permission_says_it_cannot_tell_and_names_the_grant(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            report = report_touched(
                (critical(),),
                {},
                env=pr_env(event_file(tmp)),
                list_files=lambda *_: (403, "no"),
            )

            self.assertFalse(report.known)
            self.assertIn("pull-requests: read", report.unknown_reason)

    def test_a_pull_request_touching_a_critical_file_reports_it(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            report = report_touched(
                (critical(),),
                {("POST", "/checkout"): (2, 0, 0)},
                env=pr_env(event_file(tmp)),
                list_files=files_page("src/checkout.ts", "README.md"),
            )

            self.assertTrue(report.known)
            self.assertEqual([t.endpoint.label for t in report.endpoints], ["POST /checkout"])

    def test_a_pull_request_touching_nothing_critical_reports_nothing(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            report = report_touched(
                (critical(),),
                {},
                env=pr_env(event_file(tmp)),
                list_files=files_page("README.md"),
            )

            self.assertTrue(report.known)
            self.assertEqual(report.endpoints, ())


if __name__ == "__main__":
    unittest.main()
