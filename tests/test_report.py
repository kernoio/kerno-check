from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
            env = {
                "KERNO_JUNIT_PATH": str(junit),
                "KERNO_REPLAY_EXIT": "0",
                "GITHUB_OUTPUT": str(github_output),
            }
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(report.main(), 0)
            text = github_output.read_text(encoding="utf-8")
            self.assertIn("total=2\n", text)
            self.assertIn("passed=1\n", text)
            self.assertIn("skipped=1\n", text)
            self.assertIn("portal-run-urls=\n", text)

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
            def fake_publish(cases, config, **kwargs):  # type: ignore[no-untyped-def]
                from portal import PortalRun

                return [
                    PortalRun(
                        endpoint="GET /",
                        run_report_id="run-1",
                        portal_run_url="https://portal.test/runs/run-1?org=org-1",
                    )
                ]

            with patch.dict(os.environ, env, clear=True), patch(
                "report.publish_runs", side_effect=fake_publish
            ):
                self.assertEqual(report.main(), 0)
            output = github_output.read_text(encoding="utf-8")
            self.assertIn("https://portal.test/runs/run-1?org=org-1", output)
            self.assertIn("[GET /](https://portal.test/runs/run-1?org=org-1)", step_summary.read_text())

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


if __name__ == "__main__":
    unittest.main()
