"""Fail unless action.yml's runner image default is the production latest tag.

Customers should not need to bump this action when ts-sandbox ships — they pull
`kernoio/ts-sandbox:latest` at replay time. The `image` input remains for pinning a digest or
testing unreleased builds.
"""

import re
import sys

PROD_LATEST = "kernoio/ts-sandbox:latest"
IMAGE_DEFAULT = re.compile(
    r"^\s*image:\s*\n(?:[ \t].*\n)*?\s*default:\s*(?P<ref>\S+)\s*$",
    re.M,
)


def main() -> int:
    source = open("action.yml", encoding="utf-8").read()
    match = IMAGE_DEFAULT.search(source)
    if not match:
        print(
            "::error file=action.yml::could not read the `image` input default — "
            f"it must be `{PROD_LATEST}`"
        )
        return 1

    ref = match.group("ref")
    if ref != PROD_LATEST:
        print(
            f"::error file=action.yml::the runner image default is `{ref}` — "
            f"use `{PROD_LATEST}` so customers pick up production ts-sandbox without a new "
            f"kerno-check release. Override with the `image` input to test other builds."
        )
        return 1

    print(f"runner image defaults to production latest: {ref}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
