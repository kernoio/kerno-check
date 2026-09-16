# Kerno Check

Replay your committed Kerno scenarios against a running system under test, and report them as a
pull-request check.

```yaml
name: kerno

on: pull_request

permissions:
  contents: read
  checks: write
  pull-requests: write

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
          comment: false
```

No Kerno account, no API key, no agent — unless you opt in to portal links. The action
pulls one public image and runs the scenarios already committed to your repository.

## What it does, and what it does not

It **replays scenarios that already exist** under `<app>/.kerno/scenarios` — the ones Kerno wrote
and you committed. Its exit code is your gate.

It does **not** create or update scenarios. That happens on a developer's machine, where they can
review what Kerno proposes before committing it. Nothing here calls a language model, which is
also why it is fast and free to run.

It does **not** start your application. You start it in an earlier step and pass `sut-url`. Kerno
connects to a system under test; it never manages one.

## Critical endpoints

Some endpoints matter more than the rest — checkout, auth, billing. Mark one **critical** in the
Kerno portal and the check reports what it knows about them, so a reviewer can see whether a pull
request is near anything the team said must not break.

The set is read from whichever source this run has:

| Source | When | Freshness |
| --- | --- | --- |
| `<app>/.kerno/criticality.json` in the checkout | always available; written by the agent on its last sync | as of that sync |
| the portal | when `api-key` and `organization-id` are set | current |

The portal wins when it is available. If it cannot be reached the check falls back to the committed
file, says so, and carries on — a check that goes red because the portal blinked is a check people
learn to ignore. With no account and no file, criticality is simply absent from the output rather
than reported as zero.

The summary always names the source, because "0 critical endpoints" read live and the same zero read
from a file written three weeks ago are very different facts.

## Reporting on the pull request

Three surfaces:

| Surface | Who writes it | What it is for |
|---------|----------------|----------------|
| **PR comment** | this action | Totals, then one row per portal run (pass/fail and a report link). Totals only when there are no portal URLs. Never a row per scenario. |
| **Checks tab** | `mikepenz/action-junit-report` | Per-scenario detail (expected vs actual, the failing assertion). |
| **Job summary / log** | this action | The same table as the comment, plus the raw runner output. |

The comment is the thing people read on the PR. Keep the JUnit reporter for the Checks tab, and
turn its own comment **off** — otherwise it dumps every scenario onto the PR and buries the table.

```yaml
          comment: false
```

### Permissions

```yaml
permissions:
  contents: read
  checks: write          # the JUnit reporter creates a check run
  pull-requests: write   # this action comments the table
```

A missing `pull-requests: write` is a warning, not a failed check: replay still gates the PR, the
comment just does not land.

### Secrets and variables

Create these on the repository (Settings → Secrets and variables → Actions):

| Name | Kind | |
|------|------|-|
| `KERNO_API_KEY` | **secret** | A virtual key id. Opens the portal runs. |
| `KERNO_ORGANIZATION_ID` | **variable** | The organization those runs belong to. Not a secret. |

Both must be set together, or neither. One without the other is a configuration error (exit 2)
before anything is published. Leave both unset for the no-account path: replay still runs, the
comment is only the totals, and nothing calls home.

### What the comment looks like

With portal credentials:

```markdown
**Kerno check:** 12 passed, 1 failed, 0 skipped (13 total)

| Endpoint | Passed | Failed | Report |
| --- | ---: | ---: | --- |
| `services/orders · GET /health` | 2 | 0 | [open](https://portal.kerno.io/runs/…?org=…) |
| `services/orders · POST /orders` | 4 | 1 | [open](https://portal.kerno.io/runs/…?org=…) |
| `services/billing · GET /invoices` | 6 | 0 | [open](https://portal.kerno.io/runs/…?org=…) |
```

Without credentials, or if opening a run failed, the table is omitted and only the totals line
remains. The action upserts one comment (it does not stack a new one on every push).

The linked page is the same `/runs/{id}` a generate or validate run opens. Each row is the HTTP
capture from this replay — request, response, and status — not a stub rebuilt from JUnit.

### A complete workflow

Start the application, wait until it answers, replay, comment the table, publish the Checks
report. This is the shape a monorepo with more than one service usually wants:

```yaml
name: kerno

on:
  pull_request:
    branches: [main]

permissions:
  contents: read
  checks: write
  pull-requests: write

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  kerno:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v5

      - run: docker compose up -d --wait
      - run: ./scripts/wait-for-ready.sh

      - uses: kernoio/kerno-check@v1
        with:
          apps: |
            services/orders=http://localhost:8080
            services/billing=http://localhost:8081
          forward-env: |
            DATABASE_URL
            JWT_SECRET
          report-path: kerno-reports
          api-key: ${{ secrets.KERNO_API_KEY }}
          organization-id: ${{ vars.KERNO_ORGANIZATION_ID }}
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
          JWT_SECRET: ${{ secrets.JWT_SECRET }}

      - uses: mikepenz/action-junit-report@v6
        if: always()
        with:
          report_paths: kerno-reports/*.xml
          check_name: Kerno scenarios
          comment: false
          detailed_summary: true
          include_passed: true
          fail_on_failure: false
```

`if: always()` keeps the Checks report when replay fails. `fail_on_failure: false` stops the
reporter from failing the job a second time — this action's exit code is already the gate.

A single application is the same workflow with `sut-url` instead of `apps`, and `report_paths:
kerno-junit.xml`.

### Portal hosts

Defaults point at production. Override both together for the development portal:

```yaml
          events-url: https://events.dev.kerno.io/events-service/
          portal-url: https://portal.dev.kerno.io
```

A down events-service does not fail the check. The comment then falls back to totals only.

## Scenarios that need configuration

Scenarios that read a database, mint tokens from a shared secret, or call a downstream service
need values you would never commit. Name them in `forward-env` and supply them from secrets:

```yaml
      - uses: kernoio/kerno-check@v1
        with:
          sut-url: http://localhost:8080
          forward-env: |
            DATABASE_URL
            JWT_SECRET
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
          JWT_SECRET: ${{ secrets.JWT_SECRET }}
```

Only the names listed are forwarded — the rest of the runner's environment is not. A name listed
with no value fails the step **before** the container starts, naming the variable, rather than
letting a scenario fail deep in a database step while every HTTP assertion around it passes.

## A monorepo with services on different ports

```yaml
      - uses: kernoio/kerno-check@v1
        with:
          apps: |
            services/orders=http://localhost:8080
            services/billing=http://localhost:8081
```

Each application is replayed against its own URL. Use this instead of `sut-url`; a directory here
is the one containing `.kerno`, relative to the repository root.

One report is written per application, so `report-path` is a directory in this mode:

```yaml
        with:
          apps: |
            services/orders=http://localhost:8080
            services/billing=http://localhost:8081
          report-path: kerno-reports
```

`mikepenz/action-junit-report` takes a glob, so `report_paths: kerno-reports/*.xml` picks them all
up. The `total`/`passed`/`failed`/`skipped` outputs are summed across every application.

## Inputs

| Input | Required | Default | |
|-------|----------|---------|-|
| `sut-url` | unless `apps` | | Base URL of your running application. A `localhost` URL is rewritten to `host.docker.internal`, since scenarios execute inside a container. |
| `app-dir` | no | *(repository root)* | Replay one application's scenarios. Unset discovers every `<app>/.kerno/scenarios` tree — what a monorepo usually wants. All discovered apps are replayed against the same `sut-url`. |
| `scenarios` | no | *(all)* | Glob filter on the path relative to the scenarios directory, e.g. `endpoints/GET/**`. `*` stays within a segment, `**` crosses them. |
| `image` | no | *(pinned digest)* | The runner image. Pinned by digest so a given version of this action always runs the same code. |
| `apps` | no | | One `<dir>=<url>` per line, for a monorepo whose services listen on different ports. Each application is replayed against its own URL. Mutually exclusive with `sut-url` and `app-dir`. |
| `forward-env` | no | | Environment variable names to pass through to the scenarios, one per line, with values from this step's own `env:`. Only the names listed are forwarded. A name with no value fails the step before the container starts. |
| `report-path` | no | `kerno-junit.xml` | Where the JUnit XML lands. With `apps` this is a **directory**, since the runner writes one report per application. |
| `fail-on-failure` | no | `true` | Set `false` to report without gating. |
| `api-key` | no | | Virtual key id. Together with `organization-id`, opens a portal run per endpoint and prints the URL. Leave both unset for the no-account path. |
| `organization-id` | no | | Organization the portal runs belong to. Must be set with `api-key`. |
| `events-url` | no | *(production)* | Events-service base URL. Override for development. |
| `portal-url` | no | *(production)* | Portal base URL used to build the printed links. Override for development. |

## Outputs

| Output | |
|--------|-|
| `junit-path` | Path to the JUnit report. |
| `total` | Scenarios in the report. |
| `passed` | Scenarios that passed. |
| `failed` | Scenarios that failed. |
| `skipped` | Scenarios that never executed. |
| `portal-run-urls` | Portal run URLs, one per endpoint, separated by newlines. Empty when `api-key` was not set, or when opening a run failed. |

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

## Licence

This action is [MIT licensed](LICENSE) — it is glue you run inside your own CI, so it needs terms
that permit exactly that.

That covers this repository. The runner image it pulls (`kernoio/ts-sandbox`) and the Kerno agent
that authors scenarios in the first place are separate, and are not MIT licensed.

## Requirements

A Linux runner with Docker. `ubuntu-latest` works as-is.

The action adds `--cap-add=NET_ADMIN` to the runner container so Kerno can intercept outbound
HTTPS from your application — which is how scenarios can exercise paths that call third-party APIs
without those APIs being reachable from CI.
