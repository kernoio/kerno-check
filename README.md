# Kerno Check

Replay your committed Kerno scenarios against a running system under test, and report them as a
pull-request check.

```yaml
name: kerno

on: pull_request

jobs:
  kerno:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5

      # Your application. Start it however you already do — this action only connects to it.
      - run: docker compose up -d --wait
      - run: ./scripts/wait-for-ready.sh

      - uses: kernoio/kerno-check@v1
        with:
          sut-url: http://localhost:8080

      - uses: mikepenz/action-junit-report@v6
        if: always()
        with:
          report_paths: kerno-junit.xml
```

No Kerno account, no API key, no agent. The action pulls one public image and runs the scenarios
already committed to your repository.

## What it does, and what it does not

It **replays scenarios that already exist** under `<app>/.kerno/scenarios` — the ones Kerno wrote
and you committed. Its exit code is your gate.

It does **not** create or update scenarios. That happens on a developer's machine, where they can
review what Kerno proposes before committing it. Nothing here calls a language model, which is
also why it is fast and free to run.

It does **not** start your application. You start it in an earlier step and pass `sut-url`. Kerno
connects to a system under test; it never manages one.

## Inputs

| Input | Required | Default | |
|-------|----------|---------|-|
| `sut-url` | yes | | Base URL of your running application. A `localhost` URL is rewritten to `host.docker.internal`, since scenarios execute inside a container. |
| `app-dir` | no | *(repository root)* | Replay one application's scenarios. Unset discovers every `<app>/.kerno/scenarios` tree — what a monorepo usually wants. All discovered apps are replayed against the same `sut-url`. |
| `scenarios` | no | *(all)* | Glob filter on the path relative to the scenarios directory, e.g. `endpoints/GET/**`. `*` stays within a segment, `**` crosses them. |
| `image` | no | *(pinned digest)* | The runner image. Pinned by digest so a given version of this action always runs the same code. |
| `report-path` | no | `kerno-junit.xml` | Where the JUnit XML lands. |
| `fail-on-failure` | no | `true` | Set `false` to report without gating. |

## Outputs

| Output | |
|--------|-|
| `junit-path` | Path to the JUnit report. |
| `total` | Scenarios in the report. |
| `passed` | Scenarios that passed. |
| `failed` | Scenarios that failed. |
| `skipped` | Scenarios that never executed. |

## Skipped scenarios

Two kinds of committed scenario run nothing, and both are reported **skipped** — never as a pass:

- **Blocked** — Kerno could not fulfil the scenario, and recorded why in a `.scenario.blocked`
  file next to it. The reason appears in the report.
- **Not implemented** — still a stub, with no assertions.

Skips do not fail the check: a blocked scenario is a known state, not a regression. They are
counted separately and always reported, so `30 passed, 0 failed, 17 skipped` can never be read as
`47 passed`. A suite that asserts nothing should not look like coverage.

## Exit codes

| Code | |
|------|-|
| `0` | Nothing failed. Scenarios may have been skipped — check the counts. |
| `1` | At least one scenario failed. |
| `2` | Configuration error — no scenarios found, an unparseable or empty report. Never reported as a test failure. |
| `3` | The runner could not start. A broken container must not look like a failing test. |

## Starting your application first

The action needs a URL that already answers. Two shapes that work:

**Docker Compose**

```yaml
- run: docker compose up -d --wait
- run: |
    for _ in $(seq 1 30); do
      curl -fsS -o /dev/null http://localhost:8080/health && exit 0
      sleep 2
    done
    echo "app did not become ready" >&2; exit 1
- uses: kernoio/kerno-check@v1
  with:
    sut-url: http://localhost:8080
```

**A service run directly**

```yaml
- run: ./gradlew bootRun &
- run: ./scripts/wait-for-ready.sh
- uses: kernoio/kerno-check@v1
  with:
    sut-url: http://localhost:8080
```

`compose up --wait` alone is often not enough — it waits for container health, not for your
application to be serving. Both examples above poll an endpoint as well, which is worth copying.

## Requirements

A Linux runner with Docker. `ubuntu-latest` works as-is.

The action adds `--cap-add=NET_ADMIN` to the runner container so Kerno can intercept outbound
HTTPS from your application — which is how scenarios can exercise paths that call third-party APIs
without those APIs being reachable from CI.
