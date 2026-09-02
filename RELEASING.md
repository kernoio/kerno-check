# Releasing

Customers write `uses: kernoio/kerno-check@v1`. That tag **moves**: it always points at the newest
`v1.x.y`. The version tags themselves never move, so anyone needing a frozen reference pins
`@v1.2.3` or a commit SHA.

## The order matters

This action is a thin wrapper around the `kernoio/ts-sandbox` image, and the version tag records
whatever image pin was in the tree at that commit. So the pin is bumped and merged **before** the
tag is pushed — a tag cut first would ship the previous image no matter what happens afterwards.

```
1. publish the image      (aicore: ts-sandbox-publish.yaml)
2. read its digest
3. bump the pin here      (PR, merged to main)
4. push the version tag   (release.yml does the rest)
```

### 1–2. Publish the image and read its digest

The image lives in `kernoio/aicore`. Once the intended commit is published, read the **multi-arch
manifest** digest:

```bash
docker buildx imagetools inspect kernoio/ts-sandbox:<tag> \
  --format '{{json .Manifest.Digest}}' | tr -d '"'
```

Use the manifest digest, not a per-architecture one. `aicore`'s publish workflow pushes
`<sha>-amd64` and `<sha>-arm64` separately and only creates the multi-arch manifest under a moving
tag (`dev-latest`, or `<version>` and `latest` for a prod publish) — so pinning a `-amd64` tag
would break every arm64 runner.

Confirm it resolves for both architectures before using it:

```bash
docker manifest inspect kernoio/ts-sandbox@sha256:<digest>
```

### 3. Bump the pin

Edit the `image` input's default in `action.yml`, and open a pull request. CI refuses anything that
is not a digest — see `.github/scripts/verify_image_pin.py`.

Merging runs the self-test against the new image, which is the point of doing this as its own step:
a bad image is caught here rather than by a customer.

### 4. Push the version tag

```bash
git checkout main && git pull
git tag v1.2.3 && git push origin v1.2.3
```

`release.yml` then re-verifies the pin, re-runs the self-test **against the tagged commit**, moves
`v1` to it, and creates a GitHub Release. Nothing is published if any of that fails.

## Choosing the number

Semantic, against the action's **interface** — its inputs and outputs — not against the image
inside it:

| | |
|-|-|
| **major** | an input removed or renamed, a default changed in a way that alters behaviour, or an output dropped. Requires a new moving tag (`v2`) and a note for anyone on `v1`. |
| **minor** | a new optional input, a new output, a new capability |
| **patch** | a bug fix, a docs change, or an image bump that does not change the interface |

Most releases are patches: an image bump carrying a fix from `aicore`.

This differs from the CalVer used elsewhere in the org (`kerno-agent-runtimes`, and the ts-sandbox
image tags) on purpose. Those version *artefacts a tool downloads*, where a date is a fine label.
These tags are the public API reference, and a date cannot express "still compatible with `v1`" —
which is the only question a customer pinning `@v1` is asking.

## Listing on the Marketplace

Not automated, and it cannot be: it is a checkbox on a published release in the GitHub UI
("Publish this Action to the GitHub Marketplace"). It also requires a LICENSE in the repository,
which is still outstanding.

Once listed, releases created by `release.yml` can update the listing, but the first one has to be
done by hand.
