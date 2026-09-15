from __future__ import annotations

import json
import os
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from portal import (
    DEFAULT_EVENTS_URL,
    DEFAULT_PORTAL_URL,
    ReplayCase,
    VIRTUAL_KEY_HEADER,
    group_by_endpoint,
    load_portal_config,
    parse_endpoint,
    portal_run_url,
    publish_runs,
)


def case(classname: str, name: str, status: str = "passed") -> ET.Element:
    element = ET.Element("testcase", {"classname": classname, "name": name})
    if status == "failed":
        ET.SubElement(element, "failure", {"message": "body differed"})
    elif status == "skipped":
        ET.SubElement(element, "skipped", {"message": "blocked: needs payment-service"})
    return element


def replay(classname: str, name: str, status: str = "passed", content_root: str = "app") -> ReplayCase:
    return ReplayCase(content_root=content_root, element=case(classname, name, status))


class ParseEndpointTest(unittest.TestCase):
    def test_splits_method_and_path(self) -> None:
        self.assertEqual(parse_endpoint("GET /health"), ("GET", "/health"))

    def test_a_bare_method_is_still_an_endpoint(self) -> None:
        self.assertEqual(parse_endpoint("GET"), ("GET", "/"))

    def test_blank_is_not_an_endpoint(self) -> None:
        self.assertIsNone(parse_endpoint("   "))


class GroupByEndpointTest(unittest.TestCase):
    def test_groups_scenarios_of_the_same_endpoint(self) -> None:
        grouped = group_by_endpoint(
            [
                replay("GET /health", "ok"),
                replay("GET /health", "missing"),
                replay("POST /orders", "create"),
            ]
        )
        self.assertEqual(set(grouped), {("app", "GET", "/health"), ("app", "POST", "/orders")})
        self.assertEqual(len(grouped[("app", "GET", "/health")]), 2)

    def test_the_same_path_in_two_apps_is_two_runs(self) -> None:
        grouped = group_by_endpoint(
            [
                replay("GET /health", "ok", content_root="orders"),
                replay("GET /health", "ok", content_root="billing"),
            ]
        )
        self.assertEqual(set(grouped), {("orders", "GET", "/health"), ("billing", "GET", "/health")})


class LoadPortalConfigTest(unittest.TestCase):
    def test_unset_credentials_skip_the_portal(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(load_portal_config())

    def test_one_credential_without_the_other_is_a_configuration_error(self) -> None:
        with patch.dict(os.environ, {"KERNO_API_KEY": "vk-1"}, clear=True):
            with self.assertRaises(ValueError):
                load_portal_config()
        with patch.dict(os.environ, {"KERNO_ORGANIZATION_ID": "org-1"}, clear=True):
            with self.assertRaises(ValueError):
                load_portal_config()

    def test_defaults_point_at_production(self) -> None:
        with patch.dict(
            os.environ,
            {
                "KERNO_API_KEY": "vk-1",
                "KERNO_ORGANIZATION_ID": "org-1",
                "GITHUB_REPOSITORY": "acme/shop",
                "GITHUB_REF_NAME": "main",
                "GITHUB_SHA": "abc",
            },
            clear=True,
        ):
            config = load_portal_config()
        assert config is not None
        self.assertEqual(config.events_url, DEFAULT_EVENTS_URL)
        self.assertEqual(config.portal_url, DEFAULT_PORTAL_URL)
        self.assertEqual(config.git_repo, "acme/shop")
        self.assertEqual(config.git_branch, "main")
        self.assertEqual(config.commit_sha, "abc")

    def test_a_pull_request_uses_the_head_ref(self) -> None:
        with patch.dict(
            os.environ,
            {
                "KERNO_API_KEY": "vk-1",
                "KERNO_ORGANIZATION_ID": "org-1",
                "GITHUB_HEAD_REF": "feature",
                "GITHUB_REF_NAME": "123/merge",
            },
            clear=True,
        ):
            config = load_portal_config()
        assert config is not None
        self.assertEqual(config.git_branch, "feature")


class PortalRunUrlTest(unittest.TestCase):
    def test_the_run_lives_at_runs_and_carries_the_org(self) -> None:
        self.assertEqual(
            portal_run_url("https://portal.kerno.io/", "org-1", "run-7"),
            "https://portal.kerno.io/runs/run-7?org=org-1",
        )


class PublishRunsTest(unittest.TestCase):
    def test_opens_one_run_per_endpoint_and_finishes_from_junit(self) -> None:
        posts: list[tuple[str, dict[str, str], bytes]] = []
        patches: list[tuple[str, dict[str, str], bytes]] = []

        def http_post(url: str, headers: dict[str, str], body: bytes) -> tuple[int, str]:
            posts.append((url, headers, body))
            return 201, json.dumps({"id": f"run-{len(posts)}"})

        def http_patch(url: str, headers: dict[str, str], body: bytes) -> tuple[int, str]:
            patches.append((url, headers, body))
            return 200, ""

        with patch.dict(
            os.environ,
            {
                "KERNO_API_KEY": "vk-1",
                "KERNO_ORGANIZATION_ID": "org-1",
                "KERNO_EVENTS_URL": "https://events.test/events-service/",
                "KERNO_PORTAL_URL": "https://portal.test",
                "GITHUB_REPOSITORY": "acme/shop",
                "GITHUB_REF_NAME": "main",
                "GITHUB_SHA": "deadbeef",
            },
            clear=True,
        ):
            config = load_portal_config()
        assert config is not None

        runs = publish_runs(
            [
                replay("GET /health", "ok"),
                replay("GET /health", "stub", "skipped"),
                replay("POST /orders", "create", "failed"),
            ],
            config,
            http_post=http_post,
            http_patch=http_patch,
        )

        self.assertEqual(
            [run.portal_run_url for run in runs],
            [
                "https://portal.test/runs/run-1?org=org-1",
                "https://portal.test/runs/run-2?org=org-1",
            ],
        )
        self.assertEqual(len(posts), 2)
        self.assertEqual(len(patches), 2)
        self.assertEqual(posts[0][1][VIRTUAL_KEY_HEADER], "vk-1")
        start = json.loads(posts[0][2])
        self.assertEqual(start["method"], "GET")
        self.assertEqual(start["urlPath"], "/health")
        self.assertEqual(start["status"], "running")
        self.assertEqual(start["gitRepo"], "acme/shop")
        self.assertEqual(start["contentRoot"], "app")
        finish_health = json.loads(patches[0][2])
        self.assertEqual(finish_health["outcome"], "no_diffs")
        self.assertEqual(finish_health["totalScenarios"], 2)
        self.assertEqual(finish_health["diffsDetected"], 0)
        self.assertEqual(
            {row["scenarioId"]: row["state"] for row in finish_health["report"]["scenarios"]},
            {"ok": "unchanged", "stub": "not_implemented"},
        )
        finish_orders = json.loads(patches[1][2])
        self.assertEqual(finish_orders["outcome"], "diffs_rejected")
        self.assertEqual(finish_orders["diffsDetected"], 1)
        self.assertEqual(finish_orders["report"]["scenarios"][0]["state"], "diff_detected")
        self.assertTrue(patches[0][0].endswith("/organizations/org-1/run-reports/run-1"))

    def test_a_down_events_service_does_not_raise(self) -> None:
        def http_post(url: str, headers: dict[str, str], body: bytes) -> tuple[int, str]:
            raise TimeoutError("events-service did not answer")

        with patch.dict(
            os.environ,
            {"KERNO_API_KEY": "vk-1", "KERNO_ORGANIZATION_ID": "org-1"},
            clear=True,
        ):
            config = load_portal_config()
        assert config is not None
        runs = publish_runs(
            [replay("GET /health", "ok")],
            config,
            http_post=http_post,
        )
        self.assertEqual(runs, [])

    def test_an_http_error_is_a_warning_not_a_failure(self) -> None:
        def http_post(url: str, headers: dict[str, str], body: bytes) -> tuple[int, str]:
            return 503, "unavailable"

        with patch.dict(
            os.environ,
            {"KERNO_API_KEY": "vk-1", "KERNO_ORGANIZATION_ID": "org-1"},
            clear=True,
        ):
            config = load_portal_config()
        assert config is not None
        runs = publish_runs(
            [replay("GET /health", "ok")],
            config,
            http_post=http_post,
        )
        self.assertEqual(runs, [])


if __name__ == "__main__":
    unittest.main()
