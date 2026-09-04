# Second self-test fixture

A second application, so `self-test.yml` can prove two things that a single fixture cannot:

- **`apps`** replays each application against **its own** URL. This app's scenario asserts its
  `SUT_BASE_URL` contains `8081`, the port only it is mapped to. If the action ever collapsed the
  per-app mapping back to a single `sut-url`, that assertion is the only thing that would notice.
- **`forward-env`** actually reaches a scenario. The same scenario asserts
  `KERNO_SELF_TEST_TOKEN` is present. Without forwarding it would be `undefined` while every HTTP
  assertion still passed — the silent partial pass the input exists to prevent.

Do not add scenarios here that assert on the first fixture's port, and do not "fix" this one by
relaxing either assertion: both are load-bearing.
