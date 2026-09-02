"""Fail unless action.yml pins the runner image by digest.

A floating reference like `:dev-latest` would make a released version of this action mean something
different next week — customers pin `@v1` precisely so that cannot happen. The digest is the only
reference Docker guarantees is immutable, so this is checked mechanically rather than left to
review.
"""

import re
import sys

PIN = re.compile(r"^\s*default:\s*(?P<ref>\S+/\S+@sha256:[0-9a-f]{64})\s*$", re.M)
FLOATING = re.compile(r"^\s*default:\s*(?P<ref>\S+/ts-sandbox:\S+)\s*$", re.M)


def main() -> int:
    source = open("action.yml", encoding="utf-8").read()

    floating = FLOATING.search(source)
    if floating:
        print(
            f"::error file=action.yml::the runner image is pinned to the tag "
            f"'{floating.group('ref')}' — a tag can move, so a released version would not stay "
            f"reproducible. Pin a digest instead: see RELEASING.md"
        )
        return 1

    pin = PIN.search(source)
    if not pin:
        print(
            "::error file=action.yml::no runner image pinned by digest was found. The `image` "
            "input's default must be of the form kernoio/ts-sandbox@sha256:<64 hex>"
        )
        return 1

    print(f"runner image pinned by digest: {pin.group('ref')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
