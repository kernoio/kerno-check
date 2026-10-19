# Releasing

Customers write `uses: kernoio/kerno-check@v1`. That tag **moves**: it always points at the newest
`v1.x.y`. The version tags themselves never move, so anyone needing a frozen reference pins
`@v1.2.3` or a commit SHA.

## The runner image

This action is a thin wrapper around `kernoio/ts-sandbox`. The `image` input defaults to
`kernoio/ts-sandbox:latest`, which is updated when `aicore` publishes ts-sandbox to production.
Customers on `@v1` pick up a new runner on their next replay without a new kerno-check release.

To pin a specific build, pass `image:` with a digest. CI checks that the **default** stays on
`latest` and that whatever that default resolves to at test time still writes the HTTP capture — see
`.github/scripts/verify_runner_image_default.py` and `verify_default_runner_image.py`.

## Cutting a release

```
1. merge changes to main (self-test must pass)
2. push the version tag   (release.yml does the rest)
```

```bash
git checkout main && git pull
git tag v1.2.3 && git push origin v1.2.3
```

`release.yml` re-runs the self-test **against the tagged commit**, moves `v1` to it, and creates a
GitHub Release. Nothing is published if any of that fails.

## Choosing the number

Semantic, against the action's **interface** — its inputs and outputs — not against the image
inside it:

| | |
|-|-|
| **major** | an input removed or renamed, a default changed in a way that alters behaviour, or an output dropped. Requires a new moving tag (`v2`) and a note for anyone on `v1`. |
| **minor** | a new optional input, a new output, a new capability |
| **patch** | a bug fix or a docs change |

Image fixes ship through `ts-sandbox:latest`; they do not require a kerno-check release unless the
action's own code or interface changed.

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
