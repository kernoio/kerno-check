"""Merge per-application replay captures into the one document the report step reads.

A repository-root application cannot share a discovery run with another application: the
runner keys the root as an empty relative path, so `--app=.` never matches, and it stops
walking once it has found that root. The action replays each mapped application by its own
scenarios directory instead. That invocation labels every application as the root, so each
capture would otherwise collapse into one portal row. The content root is restored from the
directory the workflow named before the captures are concatenated.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def merge_run_reports(parts: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    scenarios: list[dict[str, Any]] = []
    for content_root, document in parts:
        raw = document.get("scenarios") if isinstance(document, dict) else None
        if not isinstance(raw, list):
            continue
        for scenario in raw:
            if not isinstance(scenario, dict):
                continue
            rewritten = dict(scenario)
            rewritten["contentRoot"] = content_root
            scenarios.append(rewritten)
    return {"version": 1, "scenarios": scenarios}


def load_part(content_root: str, path: str) -> tuple[str, dict[str, Any]] | None:
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        print(f"::warning::could not read the capture for {content_root or '.'}: {error}")
        return None
    if not isinstance(document, dict):
        print(f"::warning::the capture for {content_root or '.'} is not a document")
        return None
    return content_root, document


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: captures.py <out>", file=sys.stderr)
        return 2
    parts: list[tuple[str, dict[str, Any]]] = []
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            item = json.loads(text)
        except json.JSONDecodeError as error:
            print(f"::warning::could not read a capture manifest line: {error}")
            continue
        if not isinstance(item, dict):
            continue
        content_root = item.get("contentRoot")
        path = item.get("path")
        if not isinstance(content_root, str) or not isinstance(path, str) or not path:
            print("::warning::a capture manifest line is missing contentRoot or path")
            continue
        loaded = load_part(content_root, path)
        if loaded is not None:
            parts.append(loaded)
    if not parts:
        return 0
    with open(args[0], "w", encoding="utf-8") as handle:
        json.dump(merge_run_reports(parts), handle)
        handle.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
