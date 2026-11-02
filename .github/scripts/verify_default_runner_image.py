"""Fail unless action.yml's default runner image can publish the HTTP capture.

The composite action opens portal runs from run-report.json, which only recent runner images
write. CI pulls whatever `image` defaults to and checks the image still ships capture-report.js.
"""

import re
import subprocess
import sys

IMAGE_DEFAULT = re.compile(
    r"^\s*image:\s*\n(?:[ \t].*\n)*?\s*default:\s*(?P<ref>\S+)\s*$",
    re.M,
)
CAPTURE_PUBLISHER = "/app/dist/capture-report.js"


def main() -> int:
    source = open("action.yml", encoding="utf-8").read()
    match = IMAGE_DEFAULT.search(source)
    if not match:
        print(
            "::error file=action.yml::could not read the `image` input default — "
            "cannot verify the runner image"
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
