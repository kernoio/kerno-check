#!/usr/bin/env python3
"""Rejects a ${{ }} expression in action.yml that GitHub cannot evaluate.

A composite action's `run` blocks go through the same template engine as a workflow, but with a
much smaller set of contexts: `inputs`, `steps`, `env`, `github`, `runner`, `job`, `matrix` and
`strategy`. `secrets` is NOT among them.

This exists because a literal `${{ secrets.FOO }}` written inside an error MESSAGE — as example
text for the reader — stopped the whole file loading:

    action.yml (Line: 99, Col: 12): Unrecognized named-value: 'secrets'

That is not a failing test, it is the action refusing to run at all, for every consumer. It also
cannot be caught by testing the shell: extracting a `run` block and executing it in bash never
reaches GitHub's template layer, so the expression is inert there and looks fine.
"""
from __future__ import annotations

import pathlib
import re
import sys

# Contexts a composite action may reference. Deliberately a allowlist: a new context is rarer than
# a typo, and being told to update this file is a better failure than a load error in a customer's
# workflow.
ALLOWED = {"inputs", "steps", "env", "github", "runner", "job", "matrix", "strategy"}

EXPRESSION = re.compile(r"\$\{\{(.+?)\}\}", re.DOTALL)
LEADING_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


def main() -> int:
    path = pathlib.Path(__file__).resolve().parents[2] / "action.yml"
    text = path.read_text(encoding="utf-8")

    problems: list[str] = []
    for match in EXPRESSION.finditer(text):
        expression = match.group(1).strip()
        line = text.count("\n", 0, match.start()) + 1
        name_match = LEADING_NAME.match(expression)
        if name_match is None:
            problems.append(f"{path.name}:{line}: cannot read a context name from '{expression}'")
            continue
        name = name_match.group(0)
        if name not in ALLOWED:
            problems.append(
                f"{path.name}:{line}: '{name}' is not a context a composite action can use "
                f"(in '{expression}'). If this is example text for a reader, write it without "
                f"the braces — GitHub evaluates it and the file will not load."
            )

    for problem in problems:
        print(f"::error::{problem}")
    if problems:
        return 1

    print(f"{path.name}: every ${{{{ }}}} expression uses a context a composite action can read")
    return 0


if __name__ == "__main__":
    sys.exit(main())
