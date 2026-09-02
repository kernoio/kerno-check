# test-fixture

Scenarios for this repository's own self-test workflow, not an example to copy.

`fixture_pass` must be reported passed and `fixture_skipped` must be reported skipped. The second
one is the point: a stub asserts nothing, and an action that counted it as a pass would report
coverage that does not exist. Do not implement it.

A customer's scenarios live in their own repository, written by Kerno — see the top-level README.
