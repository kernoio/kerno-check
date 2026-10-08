from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from portal import VIRTUAL_KEY_HEADER
from sync import (
    EXIT_OK,
    EXIT_USAGE,
    MAX_LISTED,
    default_branch,
    discover,
    format_summary,
    load_plan,
    main,
    parse_meta_path,
)


def scenario_source(path: str) -> str:
    return (
        "import {ctx} from '@kerno/ts-sandbox'\n\n"
        "export const meta = {\n"
        "  description: 'A path: GET /not-this-one appears in prose first',\n"
        f"  path: '{path}',\n"
        "}\n\n"
        "export default function scenario() {}\n"
    )


def write(root: Path, relative: str, content: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def plan(*entries: tuple[str, str, str]) -> str:
    return json.dumps(
        {"scenarios": [{"id": i, "spec": {"id": i, "title": title, "kind": kind}} for i, title, kind in entries]}
    )


class Recorder:
    def __init__(self, status: int = 200, body: str | None = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, str], dict]] = []
        self.status = status
        self.body = body
        self.error = error

    def __call__(self, url: str, headers: dict[str, str], body: bytes) -> tuple[int, str]:
        self.calls.append((url, headers, json.loads(body)))
        if self.error is not None:
            raise self.error
        return self.status, self.body if self.body is not None else json.dumps(
            {"snapshot": {"gitBranch": "main", "commitSha": "abc1234567", "endpoints": []}, "previous": None, "changes": {}}
        )


class ParseMetaPathTest(unittest.TestCase):
    def test_reads_the_path_declared_in_meta_not_one_mentioned_earlier(self) -> None:
        self.assertEqual(parse_meta_path(scenario_source("POST /organizations/{orgId}/runs")), ("POST", "/organizations/{orgId}/runs"))

    def test_accepts_double_quotes_and_upper_cases_the_method(self) -> None:
        source = 'export const meta = {\n  path: "get /health",\n}\n'
        self.assertEqual(parse_meta_path(source), ("GET", "/health"))

    def test_without_meta_there_is_no_endpoint(self) -> None:
        self.assertIsNone(parse_meta_path("export default function x() { const path = 'GET /x' }"))


class DefaultBranchTest(unittest.TestCase):
    def test_reads_the_default_branch_from_the_event_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event = Path(tmp) / "event.json"
            event.write_text(json.dumps({"repository": {"default_branch": "trunk"}}), encoding="utf-8")
            self.assertEqual(default_branch(str(event)), "trunk")

    def test_a_missing_or_unreadable_payload_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "event.json"
            broken.write_text("{", encoding="utf-8")
            self.assertIsNone(default_branch(str(broken)))
            self.assertIsNone(default_branch(""))


class LoadPlanTest(unittest.TestCase):
    def test_titles_and_kinds_come_from_the_spec_and_blanks_are_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "plan.json").write_text(plan(("a", "Title A", "happy_path"), ("b", " ", "")), encoding="utf-8")
            self.assertEqual(load_plan(tmp), {"a": ("Title A", "happy_path"), "b": (None, None)})

    def test_a_missing_or_broken_plan_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_plan(tmp), {})
            Path(tmp, "plan.json").write_text("not json", encoding="utf-8")
            self.assertEqual(load_plan(tmp), {})


class DiscoverTest(unittest.TestCase):
    def test_groups_scenarios_by_content_root_and_meta_path_whatever_the_folder_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            flat = "apps/events/.kerno/scenarios/endpoints/POST/runs/search"
            write(root, f"{flat}/happy_path.scenario.ts", scenario_source("POST /runs/search"))
            write(root, f"{flat}/plan.json", plan(("happy_path", "Finds runs", "happy_path")))
            write(root, f"{flat}/preconditions.ts", "export const x = 1\n")
            write(root, f"{flat}/report.json", "{}")
            write(root, f"{flat}/draft.scenario.md", "# not runnable\n")
            prefixed = "apps/events/.kerno/scenarios/endpoints/POST/events/runs/search"
            write(root, f"{prefixed}/cross_org.scenario.ts", scenario_source("POST /runs/search"))
            write(root, ".kerno/scenarios/endpoints/GET/health.scenario.ts", scenario_source("GET /health"))
            write(root, "node_modules/pkg/.kerno/scenarios/endpoints/GET/x.scenario.ts", scenario_source("GET /x"))

            discovery = discover(tmp)

        self.assertEqual(discovery.unreadable, [])
        self.assertEqual(discovery.duplicates, [])
        self.assertEqual(
            discovery.endpoints,
            [
                {
                    "contentRoot": "",
                    "method": "GET",
                    "urlPath": "/health",
                    "scenarios": [
                        {"id": "health", "filePath": ".kerno/scenarios/endpoints/GET/health.scenario.ts", "title": None, "kind": None}
                    ],
                },
                {
                    "contentRoot": "apps/events",
                    "method": "POST",
                    "urlPath": "/runs/search",
                    "scenarios": [
                        {"id": "cross_org", "filePath": f"{prefixed}/cross_org.scenario.ts", "title": None, "kind": None},
                        {"id": "happy_path", "filePath": f"{flat}/happy_path.scenario.ts", "title": "Finds runs", "kind": "happy_path"},
                    ],
                },
            ],
        )
        self.assertEqual(discovery.scenario_count, 3)

    def test_a_repeated_id_for_the_same_endpoint_is_kept_once_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "app/.kerno/scenarios/endpoints/GET/a/happy_path.scenario.ts", scenario_source("GET /a"))
            write(root, "app/.kerno/scenarios/endpoints/GET/app/a/happy_path.scenario.ts", scenario_source("GET /a"))

            discovery = discover(tmp)

        self.assertEqual(discovery.scenario_count, 1)
        self.assertEqual(discovery.duplicates, ["app/.kerno/scenarios/endpoints/GET/app/a/happy_path.scenario.ts"])

    def test_a_scenario_without_a_readable_meta_path_is_left_out_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "app/.kerno/scenarios/endpoints/GET/a/broken.scenario.ts", "export default function x() {}\n")

            discovery = discover(tmp)

        self.assertEqual(discovery.endpoints, [])
        self.assertEqual(discovery.unreadable, ["app/.kerno/scenarios/endpoints/GET/a/broken.scenario.ts"])


def sync_response(previous: dict | None, changes: dict, endpoints: list[dict]) -> dict:
    return {
        "snapshot": {"gitBranch": "main", "commitSha": "def4567890", "endpoints": endpoints},
        "previous": previous,
        "changes": changes,
    }


def endpoint(path: str, *ids: str, content_root: str = "app") -> dict:
    return {
        "contentRoot": content_root,
        "method": "POST",
        "urlPath": path,
        "scenarios": [{"id": i, "filePath": f"{content_root}/.kerno/{i}.scenario.ts", "title": f"Title {i}", "kind": None} for i in ids],
    }


class FormatSummaryTest(unittest.TestCase):
    def test_the_first_snapshot_states_totals_without_listing_everything_as_added(self) -> None:
        text = format_summary(sync_response(None, {"added": [endpoint("/a", "x")]}, [endpoint("/a", "x")]), "o/r")

        self.assertIn("First snapshot of main", text)
        self.assertIn("**1** endpoints · **1** scenarios", text)
        self.assertNotIn("Added endpoints", text)

    def test_changes_are_listed_with_deltas_and_links_to_the_right_commit(self) -> None:
        previous = {"commitSha": "abc1234567", "endpointCount": 2, "scenarioCount": 3}
        changes = {
            "added": [endpoint("/new", "happy_path")],
            "removed": [endpoint("/gone", "old")],
            "changed": [
                {
                    "contentRoot": "app",
                    "method": "POST",
                    "urlPath": "/kept",
                    "fromScenarioCount": 1,
                    "toScenarioCount": 1,
                    "addedScenarios": [{"id": "fresh", "filePath": "app/.kerno/fresh.scenario.ts", "title": None}],
                    "removedScenarios": [{"id": "stale", "filePath": "app/.kerno/stale.scenario.ts", "title": "Stale one"}],
                }
            ],
        }
        current = [endpoint("/new", "happy_path"), endpoint("/kept", "fresh")]

        with patch.dict(os.environ, {"GITHUB_SERVER_URL": "https://github.com"}):
            text = format_summary(sync_response(previous, changes, current), "o/r")

        self.assertIn("### Kerno · main @ `def4567`", text)
        self.assertIn("+0 endpoints, -1 scenarios since `abc1234`", text)
        self.assertIn("[Title happy_path](https://github.com/o/r/blob/def4567890/app/.kerno/happy_path.scenario.ts)", text)
        self.assertIn("[Title old](https://github.com/o/r/blob/abc1234567/app/.kerno/old.scenario.ts)", text)
        self.assertIn("+[fresh](https://github.com/o/r/blob/def4567890/app/.kerno/fresh.scenario.ts)", text)
        self.assertIn("−[Stale one](https://github.com/o/r/blob/abc1234567/app/.kerno/stale.scenario.ts)", text)
        self.assertIn("`POST /kept` (app) (1 → 1)", text)

    def test_no_changes_says_so(self) -> None:
        previous = {"commitSha": "abc1234567", "endpointCount": 1, "scenarioCount": 1}
        text = format_summary(sync_response(previous, {}, [endpoint("/a", "x")]), "o/r")

        self.assertIn("No scenario changes.", text)

    def test_long_lists_are_cut_off(self) -> None:
        previous = {"commitSha": "abc1234567", "endpointCount": 0, "scenarioCount": 0}
        added = [endpoint(f"/e{i}", "x") for i in range(MAX_LISTED + 5)]
        text = format_summary(sync_response(previous, {"added": added}, added), "o/r")

        self.assertIn("…and 5 more", text)
        self.assertNotIn(f"/e{MAX_LISTED}`", text)


class MainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write(self.root, "app/.kerno/scenarios/endpoints/GET/a/happy_path.scenario.ts", scenario_source("GET /a"))
        self.event = self.root / "event.json"
        self.event.write_text(json.dumps({"repository": {"default_branch": "main"}}), encoding="utf-8")
        self.summary = self.root / "summary.md"
        self.env = {
            "KERNO_API_KEY": "vk-1",
            "KERNO_ORGANIZATION_ID": "org-1",
            "KERNO_EVENTS_URL": "http://events.test/events-service/",
            "GITHUB_WORKSPACE": str(self.root),
            "GITHUB_EVENT_PATH": str(self.event),
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REF_NAME": "main",
            "GITHUB_SHA": "abc1234567",
            "GITHUB_REPOSITORY": "o/r",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_RUN_ID": "42",
            "GITHUB_STEP_SUMMARY": str(self.summary),
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_main(self, recorder: Recorder, **overrides: str) -> tuple[int, str]:
        env = {**self.env, **overrides}
        env = {key: value for key, value in env.items() if value is not None}
        output = io.StringIO()
        with patch.dict(os.environ, env, clear=True), redirect_stdout(output):
            exit_code = main(http_put=recorder)
        return exit_code, output.getvalue()

    def test_on_the_default_branch_one_snapshot_is_sent_and_summarised(self) -> None:
        recorder = Recorder()

        exit_code, _ = self.run_main(recorder)

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(len(recorder.calls), 1)
        url, headers, body = recorder.calls[0]
        self.assertEqual(url, "http://events.test/events-service/organizations/org-1/repo-snapshots")
        self.assertEqual(headers[VIRTUAL_KEY_HEADER], "vk-1")
        self.assertEqual(
            body,
            {
                "gitRepo": "o/r",
                "gitBranch": "main",
                "commitSha": "abc1234567",
                "runUrl": "https://github.com/o/r/actions/runs/42",
                "endpoints": [
                    {
                        "contentRoot": "app",
                        "method": "GET",
                        "urlPath": "/a",
                        "scenarios": [
                            {
                                "id": "happy_path",
                                "filePath": "app/.kerno/scenarios/endpoints/GET/a/happy_path.scenario.ts",
                                "title": None,
                                "kind": None,
                            }
                        ],
                    }
                ],
            },
        )
        self.assertIn("First snapshot of main", self.summary.read_text(encoding="utf-8"))

    def test_without_credentials_nothing_is_sent(self) -> None:
        recorder = Recorder()

        exit_code, _ = self.run_main(recorder, KERNO_API_KEY="", KERNO_ORGANIZATION_ID="")

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(recorder.calls, [])
        self.assertFalse(self.summary.exists())

    def test_half_the_credentials_is_a_configuration_error(self) -> None:
        recorder = Recorder()

        exit_code, output = self.run_main(recorder, KERNO_ORGANIZATION_ID="")

        self.assertEqual(exit_code, EXIT_USAGE)
        self.assertEqual(recorder.calls, [])
        self.assertIn("::error::", output)

    def test_another_branch_is_never_recorded(self) -> None:
        recorder = Recorder()

        exit_code, output = self.run_main(recorder, GITHUB_REF="refs/heads/feature", GITHUB_REF_NAME="feature")

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(recorder.calls, [])
        self.assertIn("only records main", output)

    def test_a_tag_or_pull_request_ref_is_never_recorded(self) -> None:
        recorder = Recorder()

        exit_code, _ = self.run_main(recorder, GITHUB_REF="refs/pull/7/merge", GITHUB_REF_NAME="7/merge")

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(recorder.calls, [])

    def test_an_unknown_default_branch_sends_nothing(self) -> None:
        recorder = Recorder()

        exit_code, _ = self.run_main(recorder, GITHUB_EVENT_PATH="")

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(recorder.calls, [])

    def test_a_server_error_warns_and_leaves_the_step_green(self) -> None:
        exit_code, output = self.run_main(Recorder(status=503, body="down"))

        self.assertEqual(exit_code, EXIT_OK)
        self.assertIn("::warning::could not sync the Kerno snapshot (HTTP 503)", output)

    def test_an_unreachable_server_warns_and_leaves_the_step_green(self) -> None:
        exit_code, output = self.run_main(Recorder(error=OSError("connection refused")))

        self.assertEqual(exit_code, EXIT_OK)
        self.assertIn("::warning::could not sync the Kerno snapshot: connection refused", output)


if __name__ == "__main__":
    unittest.main()
