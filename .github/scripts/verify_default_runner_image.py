"""Fail unless action.yml's default runner image can publish the HTTP capture.

The composite action opens portal runs from run-report.json, which only recent runner images
write. A digest pin in action.yml is not enough on its own — this checks the pinned image still
ships capture-report.js.
"""

import re
import subprocess
import sys

PIN = re.compile(
    r"^\s*default:\s*(?P<ref>kernoio/ts-sandbox@sha256:[0-9a-f]{64})\s*$",
    re.M,
)
CAPTURE_PUBLISHER = "/app/dist/capture-report.js"


def main() -> int:
    source = open("action.yml", encoding="utf-8").read()
    match = PIN.search(source)
    if not match:
        print(
            "::error file=action.yml::no runner image pinned by digest was found — "
            "cannot verify the default image"
        )
        return 1

    ref = match.group("ref")
    code = subprocess.call(
        ["docker", "run", "--rm", "--entrypoint", "test", ref, "-f", CAPTURE_PUBLISHER],
    )
    if code != 0:
        print(
            f"::error file=action.yml::runner image {ref} is missing {CAPTURE_PUBLISHER} — "
            "portal runs need the HTTP capture this image should publish"
        )
        return 1

    print(f"runner image {ref} includes capture publishing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
