from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from criticality import SOURCE_NONE, CriticalitySet

import report


JUNIT = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites name="kerno-scenarios" tests="2" failures="0" skipped="1">
  <testsuite name="test-fixture" tests="2" failures="0" skipped="1" time="0.010">
    <testcase name="fixture_pass" classname="GET /" time="0.010"/>
    <testcase name="fixture_skipped" classname="GET /" time="0.000">
      <skipped message="not implemented"/>
    </testcase>
  </testsuite>
</testsuites>
"""

FAILED_JUNIT = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites name="kerno-scenarios" tests="1" failures="1" skipped="0">
  <testsuite name="test-fixture" tests="1" failures="1" skipped="0" time="0.010">
    <testcase name="boom" classname="GET /" time="0.010">
      <failure message="body differed"/>
    </testcase>
  </testsuite>
</testsuites>
"""


class ReportPortalTest(unittest.TestCase):
    def test_no_credentials_leaves_the_portal_output_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            junit = Path(tmp) / "junit.xml"
            junit.write_text(JUNIT, encoding="utf-8")
            github_output = Path(tmp) / "output"
            step_summary = Path(tmp) / "summary.md"
            env = {
                "KERNO_JUNIT_PATH": str(junit),
                "KERNO_REPLAY_EXIT": "0",
                "GITHUB_OUTPUT": str(github_output),
                "GITHUB_STEP_SUMMARY": str(step_summary),
            }
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(report.main(), 0)
            text = github_output.read_text(encoding="utf-8")
            self.assertIn("total=2\n", text)
            self.assertIn("passed=1\n", text)
            self.assertIn("skipped=1\n", text)
            self.assertIn("portal-run-urls=\n", text)
            summary = step_summary.read_text(encoding="utf-8")
            self.assertIn("**Kerno check:** 1 passed, 0 failed, 1 skipped (2 total)", summary)
            self.assertNotIn("| Endpoint |", summary)

    def test_one_credential_without_the_other_is_exit_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            junit = Path(tmp) / "junit.xml"
            junit.write_text(JUNIT, encoding="utf-8")
            env = {
                "KERNO_JUNIT_PATH": str(junit),
                "KERNO_REPLAY_EXIT": "0",
                "KERNO_API_KEY": "vk-1",
            }
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(report.main(), 2)

    def test_published_urls_are_written_to_the_log_summary_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            junit = Path(tmp) / "junit.xml"
            junit.write_text(JUNIT, encoding="utf-8")
            github_output = Path(tmp) / "output"
            step_summary = Path(tmp) / "summary.md"
            env = {
                "KERNO_JUNIT_PATH": str(junit),
                "KERNO_REPLAY_EXIT": "0",
                "KERNO_API_KEY": "vk-1",
                "KERNO_ORGANIZATION_ID": "org-1",
                "GITHUB_OUTPUT": str(github_output),
                "GITHUB_STEP_SUMMARY": str(step_summary),
            }
            capture = Path(tmp) / "run-report.json"
            capture.write_text(
                '{"version":1,"scenarios":[{"contentRoot":"test-fixture","endpoint":"GET /","status":"passed","row":{"scenarioId":"ok"}}]}',
                encoding="utf-8",
            )
            env["KERNO_CAPTURE_PATH"] = str(capture)

            def fake_publish(scenarios, config, **kwargs):  # type: ignore[no-untyped-def]
                from portal import PortalRun

                self.assertEqual(scenarios[0]["row"]["scenarioId"], "ok")
                return [
                    PortalRun(
                        endpoint="GET /",
                        content_root="test-fixture",
                        passed=1,
                        failed=0,
                        skipped=1,
                        run_report_id="run-1",
                        portal_run_url="https://portal.test/runs/run-1?org=org-1",
                    )
                ]

            # Criticality is stubbed for the same reason publish_runs is: main() reaches
            # events-service for it, and a unit test must not depend on a network.
            with patch.dict(os.environ, env, clear=True), patch(
                "report.publish_runs", side_effect=fake_publish
            ), patch("report.load_criticality", return_value=CriticalitySet((), SOURCE_NONE)):
                self.assertEqual(report.main(), 0)
            output = github_output.read_text(encoding="utf-8")
            self.assertIn("https://portal.test/runs/run-1?org=org-1", output)
            summary = step_summary.read_text(encoding="utf-8")
            self.assertIn("**Kerno check:** 1 passed, 0 failed, 1 skipped (2 total)", summary)
            self.assertIn(
                "| `test-fixture · GET /` | 1 | 0 | [open](https://portal.test/runs/run-1?org=org-1) |",
                summary,
            )

    def test_a_failed_scenario_still_fails_after_a_portal_url_is_printed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            junit = Path(tmp) / "junit.xml"
            junit.write_text(FAILED_JUNIT, encoding="utf-8")
            env = {
                "KERNO_JUNIT_PATH": str(junit),
                "KERNO_REPLAY_EXIT": "0",
            }
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(report.main(), 1)

    def test_credentials_without_a_capture_do_not_invent_a_portal_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            junit = Path(tmp) / "junit.xml"
            junit.write_text(JUNIT, encoding="utf-8")
            github_output = Path(tmp) / "output"
            env = {
                "KERNO_JUNIT_PATH": str(junit),
                "KERNO_REPLAY_EXIT": "0",
                "KERNO_API_KEY": "vk-1",
                "KERNO_ORGANIZATION_ID": "org-1",
                "GITHUB_OUTPUT": str(github_output),
            }
            # Any test that sets KERNO_API_KEY must stub this: main() reads criticality from
            # events-service, and the default events URL is PRODUCTION. Unstubbed, this test made a
            # real request to it on every CI run.
            with patch.dict(os.environ, env, clear=True), patch(
                "report.publish_runs", side_effect=AssertionError("must not reconstruct from JUnit")
            ), patch("report.load_criticality", return_value=CriticalitySet((), SOURCE_NONE)):
                self.assertEqual(report.main(), 0)
            self.assertIn("portal-run-urls=\n", github_output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
