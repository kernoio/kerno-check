from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from comment import COMMENT_MARKER, format_comment, upsert_pull_request_comment
from portal import PortalRun


def run(
    *,
    endpoint: str = "GET /health",
    content_root: str = "app",
    passed: int = 1,
    failed: int = 0,
    skipped: int = 0,
    url: str = "https://portal.test/runs/run-1?org=org-1",
) -> PortalRun:
    return PortalRun(
        endpoint=endpoint,
        content_root=content_root,
        passed=passed,
        failed=failed,
        skipped=skipped,
        run_report_id="run-1",
        portal_run_url=url,
    )


class FormatCommentTest(unittest.TestCase):
    def test_without_portal_runs_the_comment_is_only_the_totals(self) -> None:
        body = format_comment(passed=187, failed=0, skipped=0, runs=[])
        self.assertIn(COMMENT_MARKER, body)
        self.assertIn("**Kerno check:** 187 passed, 0 failed, 0 skipped (187 total)", body)
        self.assertNotIn("|", body)
        self.assertNotIn("portal.test", body)

    def test_with_portal_runs_each_endpoint_is_one_row(self) -> None:
        body = format_comment(
            passed=3,
            failed=1,
            skipped=1,
            runs=[
                run(endpoint="GET /health", passed=1, skipped=1),
                run(
                    endpoint="POST /orders",
                    passed=2,
                    failed=1,
                    url="https://portal.test/runs/run-2?org=org-1",
                ),
            ],
        )
        self.assertIn("| Endpoint | Passed | Failed | Report |", body)
        self.assertIn("| `app · GET /health` | 1 | 0 | [open](https://portal.test/runs/run-1?org=org-1) |", body)
        self.assertIn(
            "| `app · POST /orders` | 2 | 1 | [open](https://portal.test/runs/run-2?org=org-1) |",
            body,
        )
        self.assertNotIn("fixture_pass", body)

    def test_a_blank_content_root_does_not_prefix_the_endpoint(self) -> None:
        body = format_comment(passed=1, failed=0, skipped=0, runs=[run(content_root="")])
        self.assertIn("| `GET /health` | 1 | 0 |", body)
        self.assertNotIn(" · ", body)


class UpsertPullRequestCommentTest(unittest.TestCase):
    def test_does_nothing_outside_a_pull_request(self) -> None:
        calls: list[object] = []
        with patch.dict(os.environ, {"GITHUB_TOKEN": "t", "GITHUB_EVENT_NAME": "push"}, clear=True):
            with patch("comment._http", side_effect=lambda *args, **kwargs: calls.append(args)):
                upsert_pull_request_comment("body")
        self.assertEqual(calls, [])

    def test_posts_when_no_kerno_comment_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event = Path(tmp) / "event.json"
            event.write_text(json.dumps({"number": 12}), encoding="utf-8")
            calls: list[tuple[str, str]] = []

            def fake_http(method: str, url: str, headers: dict[str, str], payload=None):
                calls.append((method, url))
                if method == "GET":
                    return 200, json.dumps([{"body": "unrelated", "url": "https://api.github.com/c/1"}])
                return 201, "{}"

            env = {
                "GITHUB_TOKEN": "t",
                "GITHUB_EVENT_NAME": "pull_request",
                "GITHUB_REPOSITORY": "acme/shop",
                "GITHUB_EVENT_PATH": str(event),
            }
            with patch.dict(os.environ, env, clear=True), patch("comment._http", side_effect=fake_http):
                upsert_pull_request_comment("<!-- kerno-check -->\nhello")
            self.assertEqual(
                calls,
                [
                    ("GET", "https://api.github.com/repos/acme/shop/issues/12/comments?per_page=100"),
                    ("POST", "https://api.github.com/repos/acme/shop/issues/12/comments"),
                ],
            )

    def test_patches_an_existing_kerno_comment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event = Path(tmp) / "event.json"
            event.write_text(json.dumps({"pull_request": {"number": 12}}), encoding="utf-8")
            calls: list[tuple[str, str]] = []

            def fake_http(method: str, url: str, headers: dict[str, str], payload=None):
                calls.append((method, url))
                if method == "GET":
                    return 200, json.dumps(
                        [
                            {
                                "body": "<!-- kerno-check -->\nold",
                                "url": "https://api.github.com/repos/acme/shop/issues/comments/99",
                            }
                        ]
                    )
                return 200, "{}"

            env = {
                "GITHUB_TOKEN": "t",
                "GITHUB_EVENT_NAME": "pull_request",
                "GITHUB_REPOSITORY": "acme/shop",
                "GITHUB_EVENT_PATH": str(event),
            }
            with patch.dict(os.environ, env, clear=True), patch("comment._http", side_effect=fake_http):
                upsert_pull_request_comment("<!-- kerno-check -->\nnew")
            self.assertEqual(calls[1], ("PATCH", "https://api.github.com/repos/acme/shop/issues/comments/99"))


if __name__ == "__main__":
    unittest.main()
