from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from captures import main, merge_run_reports
from comment import format_comment
from portal import PortalRun, group_by_endpoint


def scenario(endpoint: str, *, name: str) -> dict:
    return {
        "contentRoot": "",
        "endpoint": endpoint,
        "status": "passed",
        "row": {"scenarioId": name},
    }


class MergeRunReportsTest(unittest.TestCase):
    def test_each_application_keeps_the_directory_the_workflow_named(self) -> None:
        # scenarios-dir replay writes every capture as the root. Restoring the directory is
        # what makes the same endpoint in two applications two rows of the PR comment.
        merged = merge_run_reports(
            [
                ("", {"version": 1, "scenarios": [scenario("GET /", name="root")]}),
                (
                    "superset/mcp_service",
                    {"version": 1, "scenarios": [scenario("GET /", name="mcp")]},
                ),
            ]
        )

        grouped = group_by_endpoint(merged["scenarios"])
        self.assertEqual(
            set(grouped),
            {("", "GET", "/"), ("superset/mcp_service", "GET", "/")},
        )
        runs = [
            PortalRun(
                endpoint=f"{method} {path}",
                content_root=content_root,
                passed=len(group),
                failed=0,
                skipped=0,
                run_report_id="run-1",
                portal_run_url="https://portal.test/runs/run-1?org=org-1",
            )
            for (content_root, method, path), group in sorted(grouped.items())
        ]
        body = format_comment(passed=2, failed=0, skipped=0, runs=runs)
        self.assertIn("| `GET /` | 1 | 0 |", body)
        self.assertIn("| `superset/mcp_service · GET /` | 1 | 0 |", body)

    def test_a_capture_without_scenarios_adds_nothing(self) -> None:
        merged = merge_run_reports(
            [
                ("", {"version": 1}),
                ("other", {"version": 1, "scenarios": ["not-a-scenario", scenario("GET /health", name="ok")]}),
            ]
        )
        self.assertEqual([item["row"]["scenarioId"] for item in merged["scenarios"]], ["ok"])
        self.assertEqual(merged["scenarios"][0]["contentRoot"], "other")


class MergeCliTest(unittest.TestCase):
    def test_writes_the_merged_document_and_skips_a_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "root.json"
            capture.write_text(
                json.dumps({"version": 1, "scenarios": [scenario("GET /", name="root")]}),
                encoding="utf-8",
            )
            out = root / "run-report.json"
            manifest = "\n".join(
                [
                    json.dumps({"contentRoot": "", "path": str(capture)}),
                    json.dumps({"contentRoot": "other", "path": str(root / "missing.json")}),
                ]
            )
            with patch.object(sys, "stdin", io.StringIO(manifest)):
                status = main([str(out)])
            self.assertEqual(status, 0)
            written = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(len(written["scenarios"]), 1)
            self.assertEqual(written["scenarios"][0]["contentRoot"], "")

    def test_writes_nothing_when_every_capture_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run-report.json"
            manifest = json.dumps({"contentRoot": "", "path": str(Path(tmp) / "missing.json")})
            with patch.object(sys, "stdin", io.StringIO(manifest)):
                status = main([str(out)])
            self.assertEqual(status, 0)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
