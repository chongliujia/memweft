# SQLite schema v3 upgrade and recovery

This guide applies to the September 2026 exact-bitmap build. **v3 is the recall
index schema version, not the SDK package version**: the current preview packages
report `0.2.0-alpha.1` (Python `0.2.0a1`); earlier development builds used `0.1.0`.
This preview adds required-fact retrieval without another schema migration.
Keep the Git revision and native artifacts with each
deployment so that the old and new builds can be identified independently.

All Rust, Python, Node and CLI clients open the same SQLite schema. The first
open with the new build automatically migrates it; there is no separate migration
command or opt-out. The v3 migration preserves lexical ranking. The current
preview adds requirements diagnostics to context reports; Rust callers using
explicit `ContextOptions` struct literals must supply `required_fact_keys` or
use `..Default::default()`.

| Existing database | First open with this build | Old build after migration |
|---|---|---|
| No recall index | Backfill postings and bitmaps from authoritative facts | Unsupported |
| Index v1 | Reorder postings, then backfill bitmaps | Rejected by v1/v2 SDKs |
| Index v2 | Retain postings and item IDs; backfill bitmaps | Rejected by v1/v2 SDKs |
| Index v3 | Reuse the existing index | Use a v3-capable build |

Recall migration runs in one immediate transaction. Failure rolls back its
version, tables, triggers and backfill. Other initialization, including document
indexes, is separate; this is not a promise that every byte of the database stays
unchanged after an unsuccessful SDK open. Do not downgrade by changing the version
row or deleting internal index tables.

## 1. Prepare the maintenance window

Build the new native artifacts before touching persistent data, and retain the
previous build and its configuration. Rehearse on a disposable copy with the
application's actual scopes, memory pools and queries. Confirm available disk
space for the backup, new index, temporary grouping and migration WAL. A v1
upgrade additionally needs space for both postings layouts.

The local million-fact v2 fixture grew by 8.1% and took 3.80 seconds for first
open/upgrade/close. These are workload-specific measurements, not peak-space or
startup-time bounds. [Measured migration and write costs](../evals/reports/2026-09-28-bitmap-recall.md)

Stop request admission and close **all** clients, workers, background jobs and
other processes using the file before migration. Do not leave an old reader or
writer running while another process upgrades it. Plan for SQLite's write lock
and existing busy timeout; do the first open before admitting concurrent writes.

## 2. Make and verify a backup

With all clients stopped, use SQLite's backup API rather than copying only the
main file while a WAL may contain committed data. This example reads an existing
source, refuses to overwrite a backup and checks the resulting database. Replace
the two paths with your application paths; run with a Python interpreter providing
`sqlite3` and without `-O` so validation assertions remain enabled.

```bash
python - data/memweft.db data/backups/memweft-before-v3.db <<'PY'
from contextlib import closing
from pathlib import Path
import sqlite3
import sys

source = Path(sys.argv[1]).resolve(strict=True)
backup = Path(sys.argv[2]).resolve()
backup.parent.mkdir(parents=True, exist_ok=True)
with backup.open("xb"):
    pass
with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
    with closing(sqlite3.connect(backup)) as dst:
        src.backup(dst)
        assert dst.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert dst.execute("PRAGMA foreign_key_check").fetchone() is None
        indexed = dst.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                              "AND name='memweft_recall_version'").fetchone()
        version = dst.execute("SELECT version FROM memweft_recall_version").fetchall() if indexed else []
print({"verified_backup": str(backup), "recall_version": version or "legacy"})
PY
```

Proceed only after verification succeeds. Keep this backup untouched, together
with the old executable/native packages, application configuration and build
identity. Capture representative contexts using the old SDK on a separate copy
for comparison; do not open the retained backup with the new SDK.

## 3. Migrate once, verify, then resume

Use an environment containing the new Python native extension for this example.
It resolves the database path first to avoid silently creating a fresh database
at a misspelled path. Rust or Node applications can perform the same first open
with their new build.

```bash
python - data/memweft.db <<'PY'
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
from memweft import Memory

database = Path(sys.argv[1]).resolve(strict=True)
with Memory(str(database)) as memory:
    print(memory.storage_status())
with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
    assert conn.execute("SELECT version FROM memweft_recall_version").fetchall() == [(3,)]
    assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert conn.execute("PRAGMA foreign_key_check").fetchone() is None
print("Schema v3 and SQLite integrity verified")
PY
```

Before resuming traffic, compare representative contexts and recent conversation
windows with the captured baseline. Exercise a disposable fact's create, update
and forget operations through the SDK, and check shared-pool revision conflicts
and source invalidation where the application uses them. A SQLite integrity check
alone does not validate application behavior.

Start only v3-capable clients. If background maintenance is enabled, use the same
configuration in every participating process and check `storage_status()` for
maintenance errors and WAL growth. The reclamation threshold is soft; long read
snapshots can keep WAL space above it. Raw SQLite writes without the SDK's
registered tokenizer function remain unsupported.

## 4. Recover with the old build if necessary

Stop admission and close every process again. Preserve the upgraded database and
its associated WAL/SHM files for diagnosis after all handles are closed; never
delete or replace them under an open client.

Restore the verified pre-upgrade backup to a **new, unused database path** using
the backup command above with the backup as its source. Configure the retained
old build to open that restored path, verify the expected schema and application
contexts, then resume traffic. An unused path avoids mixing restored data with
sidecars from the upgraded database. Do not point the old build at the v3 file or
run old and new deployments against different copies as if they were one store.

Restoring a backup returns data to its capture time. Writes accepted after that
point are absent from the restored database; preserve the upgraded copy and
reconcile any required writes through the supported application API before
declaring recovery complete. There is no in-place v3-to-v2 downgrade or automatic
write replay. Rehearse this recovery before deploying to persistent business data.

## Evidence and remaining acceptance work

Migration tests cover legacy/v1/v2 inputs, rollback after schema creation and
backfill, signed bitmap bits, reused connections and cascaded deletion. The local
query comparison checks 30 complete contexts; a five-minute multiwriter run checks
snapshot stability, acknowledged writes and idle recovery with clients open.
On 2026-09-28, the exact backup and migration snippets above were also exercised
locally with the archived v2 binary and current v3 binary: three complete contexts
matched after migration and after restoring to a new path, the old binary rejected
v3, and the examples refused an existing backup or a missing source database.
These do not replace cross-platform installation checks, application-specific
upgrade rehearsals or hours-long storage tests.

- [Index semantics and migration internals](indexed_retrieval.md)
- [Conversation indexes and source invalidation](long_conversations.md)
- [Checkpoint coordination and durability limits](async_pipeline.md)
- [Benchmark reproduction commands](../evals/README.md#exact-bitmap-index-comparison)
