from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import io
from contextlib import redirect_stdout

from fakes import API_KEY, ORGANIZATION_ID, VIRTUAL_KEY_ID, FakeExchange
from portal import (
    DEFAULT_BACKEND_URL,
    DEFAULT_EVENTS_URL,
    DEFAULT_PORTAL_URL,
    VIRTUAL_KEY_HEADER,
    PortalConfig,
    group_by_endpoint,
    load_capture,
    load_credentials,
    load_portal_config,
    parse_endpoint,
    portal_run_url,
    publish_runs,
)


def portal_config(**overrides: str) -> PortalConfig:
    values = {
        "virtual_key_id": VIRTUAL_KEY_ID,
        "organization_id": ORGANIZATION_ID,
        "events_url": "https://events.test/events-service/",
        "portal_url": "https://portal.test",
        "git_repo": "acme/shop",
        "git_branch": "main",
        "commit_sha": "deadbeef",
    }
    return PortalConfig(**{**values, **overrides})


def scenario(
    *,
    name: str,
    endpoint: str,
    status: str = "passed",
    content_root: str = "app",
    row: dict | None = None,
) -> dict:
    payload = {
        "contentRoot": content_root,
        "endpoint": endpoint,
        "status": status,
        "row": row
        if row is not None
        else {
            "scenarioId": name,
            "scenario": name,
            "verdict": "passed" if status == "passed" else ("not_implemented" if status == "skipped" else "failed"),
            "state": "unchanged"
            if status == "passed"
            else ("not_implemented" if status == "skipped" else "diff_detected"),
            "hasErrors": status == "failed",
            "requestHeaders": {"content-type": "application/json"},
            "responseHeaders": {"content-type": "application/json"},
            "requestBody": '{"ok":true}',
            "responseBody": '{"id":1}',
            "responseStatus": 200,
            "stepsValidation": [],
        },
    }
    if status == "skipped":
        payload["row"].pop("responseStatus", None)
        payload["row"]["requestBody"] = ""
        payload["row"]["responseBody"] = ""
    return payload


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
                scenario(name="ok", endpoint="GET /health"),
                scenario(name="missing", endpoint="GET /health"),
                scenario(name="create", endpoint="POST /orders"),
            ]
        )
        self.assertEqual(set(grouped), {("app", "GET", "/health"), ("app", "POST", "/orders")})
        self.assertEqual(len(grouped[("app", "GET", "/health")]), 2)

    def test_the_same_path_in_two_apps_is_two_runs(self) -> None:
        grouped = group_by_endpoint(
            [
                scenario(name="ok", endpoint="GET /health", content_root="orders"),
                scenario(name="ok", endpoint="GET /health", content_root="billing"),
            ]
        )
        self.assertEqual(set(grouped), {("orders", "GET", "/health"), ("billing", "GET", "/health")})


class LoadCaptureTest(unittest.TestCase):
    def test_missing_path_is_none(self) -> None:
        self.assertIsNone(load_capture(""))
        self.assertIsNone(load_capture("/no/such/file.json"))

    def test_reads_the_runner_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run-report.json"
            path.write_text(
                json.dumps({"version": 1, "scenarios": [scenario(name="ok", endpoint="GET /")]}),
                encoding="utf-8",
            )
            loaded = load_capture(str(path))
        assert loaded is not None
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["row"]["responseStatus"], 200)


class LoadCredentialsTest(unittest.TestCase):
    def load(self, env: dict[str, str], exchange: FakeExchange) -> tuple[object, str]:
        output = io.StringIO()
        with patch.dict(os.environ, env, clear=True), redirect_stdout(output):
            credentials = load_credentials(exchange)
        return credentials, output.getvalue()

    def test_without_an_api_key_nothing_is_exchanged(self) -> None:
        exchange = FakeExchange()
        credentials, _ = self.load({}, exchange)
        self.assertIsNone(credentials)
        self.assertEqual(exchange.calls, [])

    def test_an_organization_without_an_api_key_is_a_configuration_error(self) -> None:
        with patch.dict(os.environ, {"KERNO_ORGANIZATION_ID": ORGANIZATION_ID}, clear=True):
            with self.assertRaises(ValueError):
                load_credentials(FakeExchange())

    def test_the_api_key_alone_resolves_the_key_id_and_organization(self) -> None:
        exchange = FakeExchange()
        credentials, _ = self.load({"KERNO_API_KEY": API_KEY}, exchange)

        self.assertEqual(credentials.virtual_key_id, VIRTUAL_KEY_ID)
        self.assertEqual(credentials.organization_id, ORGANIZATION_ID)
        self.assertEqual(credentials.events_url, DEFAULT_EVENTS_URL)
        url, headers, body = exchange.calls[0]
        self.assertEqual(url, f"{DEFAULT_BACKEND_URL}/api/api-keys/exchange")
        self.assertEqual(headers, {"Content-Type": "application/json"})
        self.assertEqual(body, {"apiKey": API_KEY, "organizationId": None})

    def test_a_given_organization_and_backend_are_passed_to_the_exchange(self) -> None:
        exchange = FakeExchange()
        self.load(
            {
                "KERNO_API_KEY": API_KEY,
                "KERNO_ORGANIZATION_ID": ORGANIZATION_ID,
                "KERNO_BACKEND_URL": "https://api.dev.test/",
            },
            exchange,
        )
        url, _, body = exchange.calls[0]
        self.assertEqual(url, "https://api.dev.test/api/api-keys/exchange")
        self.assertEqual(body["organizationId"], ORGANIZATION_ID)

    def test_a_rejected_key_warns_without_printing_it(self) -> None:
        credentials, output = self.load({"KERNO_API_KEY": API_KEY}, FakeExchange(status=401, body="{}"))

        self.assertIsNone(credentials)
        self.assertIn("::warning::Kerno did not accept api-key", output)
        self.assertIn("Settings → API key", output)
        self.assertNotIn(API_KEY, output)

    def test_a_server_error_warns(self) -> None:
        credentials, output = self.load({"KERNO_API_KEY": API_KEY}, FakeExchange(status=503, body="down"))
        self.assertIsNone(credentials)
        self.assertIn("(HTTP 503)", output)

    def test_an_unreachable_backend_warns(self) -> None:
        credentials, output = self.load(
            {"KERNO_API_KEY": API_KEY}, FakeExchange(error=OSError("connection refused"))
        )
        self.assertIsNone(credentials)
        self.assertIn("::warning::could not reach Kerno", output)

    def test_an_answer_without_a_key_id_warns(self) -> None:
        credentials, output = self.load(
            {"KERNO_API_KEY": API_KEY}, FakeExchange(body=json.dumps({"organizationId": ORGANIZATION_ID}))
        )
        self.assertIsNone(credentials)
        self.assertIn("did not say which key", output)


class LoadPortalConfigTest(unittest.TestCase):
    def test_unset_credentials_skip_the_portal(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(load_portal_config(FakeExchange()))

    def test_defaults_point_at_production(self) -> None:
        with patch.dict(
            os.environ,
            {
                "KERNO_API_KEY": API_KEY,
                "GITHUB_REPOSITORY": "acme/shop",
                "GITHUB_REF_NAME": "main",
                "GITHUB_SHA": "abc",
            },
            clear=True,
        ):
            config = load_portal_config(FakeExchange())
        assert config is not None
        self.assertEqual(config.virtual_key_id, VIRTUAL_KEY_ID)
        self.assertEqual(config.organization_id, ORGANIZATION_ID)
        self.assertEqual(config.events_url, DEFAULT_EVENTS_URL)
        self.assertEqual(config.portal_url, DEFAULT_PORTAL_URL)
        self.assertEqual(config.git_repo, "acme/shop")
        self.assertEqual(config.git_branch, "main")
        self.assertEqual(config.commit_sha, "abc")

    def test_a_pull_request_uses_the_head_ref(self) -> None:
        with patch.dict(
            os.environ,
            {
                "KERNO_API_KEY": API_KEY,
                "GITHUB_HEAD_REF": "feature",
                "GITHUB_REF_NAME": "123/merge",
            },
            clear=True,
        ):
            config = load_portal_config(FakeExchange())
        assert config is not None
        self.assertEqual(config.git_branch, "feature")


class PortalRunUrlTest(unittest.TestCase):
    def test_the_run_lives_at_runs_and_carries_the_org(self) -> None:
        self.assertEqual(
            portal_run_url("https://portal.kerno.io/", "org-1", "run-7"),
            "https://portal.kerno.io/runs/run-7?org=org-1",
        )


class PublishRunsTest(unittest.TestCase):
    def test_opens_one_run_per_endpoint_and_finishes_from_the_capture(self) -> None:
        posts: list[tuple[str, dict[str, str], bytes]] = []
        patches: list[tuple[str, dict[str, str], bytes]] = []

        def http_post(url: str, headers: dict[str, str], body: bytes) -> tuple[int, str]:
            posts.append((url, headers, body))
            return 201, json.dumps({"id": f"run-{len(posts)}"})

        def http_patch(url: str, headers: dict[str, str], body: bytes) -> tuple[int, str]:
            patches.append((url, headers, body))
            return 200, ""

        config = portal_config()

        ok = scenario(name="ok", endpoint="GET /health")
        stub = scenario(name="stub", endpoint="GET /health", status="skipped")
        create = scenario(name="create", endpoint="POST /orders", status="failed")
        runs = publish_runs(
            [ok, stub, create],
            config,
            http_post=http_post,
            http_patch=http_patch,
        )

        self.assertEqual(
            [(run.endpoint, run.passed, run.failed, run.skipped, run.portal_run_url) for run in runs],
            [
                ("GET /health", 1, 0, 1, "https://portal.test/runs/run-1?org=org-1"),
                ("POST /orders", 0, 1, 0, "https://portal.test/runs/run-2?org=org-1"),
            ],
        )
        self.assertEqual(len(posts), 2)
        self.assertEqual(len(patches), 2)
        self.assertEqual(posts[0][1][VIRTUAL_KEY_HEADER], VIRTUAL_KEY_ID)
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
        passed_row = finish_health["report"]["scenarios"][0]
        self.assertEqual(passed_row["responseStatus"], 200)
        self.assertEqual(passed_row["requestBody"], '{"ok":true}')
        self.assertEqual(passed_row["responseBody"], '{"id":1}')
        self.assertNotIn("responseStatus", finish_health["report"]["scenarios"][1])
        finish_orders = json.loads(patches[1][2])
        self.assertEqual(finish_orders["outcome"], "diffs_rejected")
        self.assertEqual(finish_orders["diffsDetected"], 1)
        self.assertEqual(finish_orders["report"]["scenarios"][0]["state"], "diff_detected")
        self.assertEqual(finish_orders["report"]["scenarios"][0]["responseStatus"], 200)
        self.assertTrue(patches[0][0].endswith("/organizations/org-1/run-reports/run-1"))

    def test_does_not_invent_rows_when_the_capture_has_none(self) -> None:
        def http_post(url: str, headers: dict[str, str], body: bytes) -> tuple[int, str]:
            raise AssertionError("must not open a run without a capture row")

        config = portal_config()
        runs = publish_runs(
            [{"contentRoot": "app", "endpoint": "GET /health", "status": "passed"}],
            config,
            http_post=http_post,
        )
        self.assertEqual(runs, [])

    def test_a_down_events_service_does_not_raise(self) -> None:
        def http_post(url: str, headers: dict[str, str], body: bytes) -> tuple[int, str]:
            raise TimeoutError("events-service did not answer")

        config = portal_config()
        runs = publish_runs(
            [scenario(name="ok", endpoint="GET /health")],
            config,
            http_post=http_post,
        )
        self.assertEqual(runs, [])

    def test_an_http_error_is_a_warning_not_a_failure(self) -> None:
        def http_post(url: str, headers: dict[str, str], body: bytes) -> tuple[int, str]:
            return 503, "unavailable"

        config = portal_config()
        runs = publish_runs(
            [scenario(name="ok", endpoint="GET /health")],
            config,
            http_post=http_post,
        )
        self.assertEqual(runs, [])


if __name__ == "__main__":
    unittest.main()
