# Sync discovery fixtures

Each file is one repository checkout and what `discover()` reads from it: `files` holds UTF-8 text by
path, `binaryFiles` holds base64 for content that is not, and `expected` is the result.

events-service builds the same snapshot from a commit's files for the GitHub App (`KernoSnapshotBuilder`
in `kernoio/becore`), and copies these files as its golden tests. A change to discovery updates both
implementations together, and the fixtures with them.

Regenerate `expected` after a deliberate change, then review the diff:

```bash
cd tests && KERNO_UPDATE_SYNC_FIXTURES=1 PYTHONPATH=.. python3 -m unittest test_sync.SyncFixturesTest
```
