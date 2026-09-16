from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from criticality import (  # noqa: E402
    SOURCE_FILE,
    SOURCE_LIVE,
    SOURCE_NONE,
    CriticalEndpoint,
    describe,
    fetch_live,
    load_criticality,
    load_from_files,
)
from portal import PortalConfig  # noqa: E402


def config() -> PortalConfig:
    return PortalConfig(
        api_key="vk-1",
        organization_id="org-1",
        events_url="https://events.test/events-service/",
        portal_url="https://portal.test",
        git_repo="kernoio/becore",
        git_branch="main",
        commit_sha="abc123",
    )


def write_file(workspace: Path, app_dir: str, *entries: dict) -> None:
    kerno = workspace / app_dir / ".kerno" if app_dir else workspace / ".kerno"
    kerno.mkdir(parents=True, exist_ok=True)
    (kerno / "criticality.json").write_text(
        json.dumps({"gitRepo": "kernoio/becore", "endpoints": list(entries)}),
        encoding="utf-8",
    )


def entry(method: str, url_path: str, content_root: str = "", filepath: str = "src/x.ts") -> dict:
    return {
        "contentRoot": content_root,
        "method": method,
        "urlPath": url_path,
        "filepath": filepath,
    }


class LoadFromFilesTest(unittest.TestCase):
    def test_finds_every_application_s_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            write_file(workspace, "", entry("POST", "/checkout"))
            write_file(workspace, "apps/api", entry("GET", "/users", content_root="apps/api"))

            found = load_from_files(str(workspace))

            self.assertEqual(
                sorted((e.content_root, e.label) for e in found),
                [("", "POST /checkout"), ("apps/api", "GET /users")],
            )

    def test_an_unreadable_file_does_not_hide_the_other_applications(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            write_file(workspace, "apps/api", entry("GET", "/users", content_root="apps/api"))
            broken = workspace / "apps" / "worker" / ".kerno"
            broken.mkdir(parents=True)
            (broken / "criticality.json").write_text("{ not json", encoding="utf-8")

            found = load_from_files(str(workspace))

            # One damaged file is not a reason to forget every mark in the repository, nor to
            # fail the check.
            self.assertEqual([e.label for e in found], ["GET /users"])

    def test_a_method_is_normalised_but_a_path_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            write_file(workspace, "", entry("post", "/Checkout"))

            found = load_from_files(str(workspace))

            self.assertEqual(found[0].method, "POST")
            self.assertEqual(found[0].url_path, "/Checkout")

    def test_an_entry_without_a_path_is_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            write_file(workspace, "", entry("POST", ""), entry("GET", "/health"))

            self.assertEqual([e.label for e in load_from_files(str(workspace))], ["GET /health"])

    def test_vendored_trees_are_not_walked(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            write_file(workspace, "node_modules/some-package", entry("GET", "/borrowed"))

            # A dependency's committed fixture is not this repository's marks.
            self.assertEqual(load_from_files(str(workspace)), [])

    def test_nothing_found_is_an_empty_list_not_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            self.assertEqual(load_from_files(raw), [])


class FetchLiveTest(unittest.TestCase):
    def test_reads_the_marks_and_scopes_them_to_the_repository(self) -> None:
        seen: dict[str, str] = {}

        def http_get(url, headers, body):
            seen["url"] = url
            seen["key"] = headers["x-kerno-virtual-key-id"]
            return 200, json.dumps(
                [
                    {
                        "gitRepo": "kernoio/becore",
                        "contentRoot": "",
                        "method": "POST",
                        "urlPath": "/checkout",
                        "filepath": "src/checkout.ts",
                    }
                ]
            )

        found = fetch_live(config(), http_get=http_get)

        self.assertEqual(found, [CriticalEndpoint("", "POST", "/checkout", "src/checkout.ts")])
        self.assertIn("/organizations/org-1/endpoints/critical", seen["url"])
        self.assertIn("gitRepo=kernoio%2Fbecore", seen["url"])
        self.assertEqual(seen["key"], "vk-1")

    def test_an_endpoint_with_no_known_location_is_kept(self) -> None:
        def http_get(url, headers, body):
            return 200, json.dumps(
                [{"contentRoot": "", "method": "GET", "urlPath": "/health", "filepath": ""}]
            )

        # Dropping it would under-report — a reviewer has to be able to be told that a critical
        # endpoint was touched even when Kerno cannot say which file it lives in.
        self.assertEqual(fetch_live(config(), http_get=http_get)[0].filepath, "")

    def test_an_error_response_is_not_an_empty_set(self) -> None:
        self.assertIsNone(fetch_live(config(), http_get=lambda *_: (500, "boom")))

    def test_an_unreachable_portal_is_not_an_empty_set(self) -> None:
        def http_get(*_):
            raise OSError("connection refused")

        # None means "this run does not know". Returning [] would say the organization has marked
        # nothing, which is a claim nobody made.
        self.assertIsNone(fetch_live(config(), http_get=http_get))

    def test_an_unparseable_body_is_not_an_empty_set(self) -> None:
        self.assertIsNone(fetch_live(config(), http_get=lambda *_: (200, "<html>nope</html>")))

    def test_a_body_that_is_not_a_list_is_not_an_empty_set(self) -> None:
        self.assertIsNone(fetch_live(config(), http_get=lambda *_: (200, '{"endpoints": []}')))


class LoadCriticalityTest(unittest.TestCase):
    def test_the_portal_wins_over_the_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            write_file(workspace, "", entry("GET", "/stale"))

            def http_get(*_):
                return 200, json.dumps([entry("POST", "/fresh")])

            loaded = load_criticality(str(workspace), config(), http_get=http_get)

            self.assertEqual(loaded.source, SOURCE_LIVE)
            self.assertEqual([e.label for e in loaded.endpoints], ["POST /fresh"])

    def test_an_unreachable_portal_falls_back_to_the_checkout_and_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            write_file(workspace, "", entry("GET", "/stale"))

            loaded = load_criticality(str(workspace), config(), http_get=lambda *_: (503, ""))

            self.assertEqual(loaded.source, SOURCE_FILE)
            self.assertEqual([e.label for e in loaded.endpoints], ["GET /stale"])
            self.assertTrue(loaded.stale_reason)
            self.assertIn("could not be reached", describe(loaded))

    def test_with_no_account_the_checkout_is_the_answer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            write_file(workspace, "", entry("POST", "/checkout"))

            loaded = load_criticality(str(workspace), None)

            self.assertEqual(loaded.source, SOURCE_FILE)
            self.assertFalse(loaded.stale_reason)

    def test_with_no_account_and_no_file_the_feature_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            loaded = load_criticality(raw, None)

            # Absent, not empty: nothing has ever told this repository what is critical, and
            # "0 critical endpoints" would read as though somebody had looked.
            self.assertEqual(loaded.source, SOURCE_NONE)
            self.assertEqual(loaded.endpoints, ())

    def test_a_portal_that_answers_nothing_marked_does_not_fall_back(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            write_file(workspace, "", entry("GET", "/stale"))

            loaded = load_criticality(str(workspace), config(), http_get=lambda *_: (200, "[]"))

            # An empty answer IS an answer — everything was unmarked in the portal, and the stale
            # file must not resurrect it.
            self.assertEqual(loaded.source, SOURCE_LIVE)
            self.assertEqual(loaded.endpoints, ())


class DescribeTest(unittest.TestCase):
    def test_names_the_source_so_a_zero_can_be_read_correctly(self) -> None:
        from criticality import CriticalitySet

        live = CriticalitySet((), SOURCE_LIVE)
        checkout = CriticalitySet((), SOURCE_FILE)

        self.assertIn("from the portal", describe(live))
        self.assertIn("from the checkout", describe(checkout))

    def test_refuses_to_report_a_count_it_never_looked_for(self) -> None:
        from criticality import CriticalitySet

        # The caller stays quiet for this source. describe() must not hand it a sentence that
        # reads as though somebody checked and found none.
        self.assertNotIn("0", describe(CriticalitySet((), SOURCE_NONE)))

    def test_counts_one_endpoint_in_the_singular(self) -> None:
        from criticality import CriticalitySet

        one = CriticalitySet((CriticalEndpoint("", "GET", "/a", ""),), SOURCE_LIVE)

        self.assertIn("1 critical endpoint (", describe(one))


if __name__ == "__main__":
    unittest.main()
