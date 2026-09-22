# MemWeft SQLite pin

This is `libsqlite3-sys` 0.28.0 from the Cargo registry, retaining its MIT license
and Rust API so rusqlite 0.31 / r2d2_sqlite 0.24 need no unrelated upgrade.

The bundled **SQLite** amalgamation (`sqlite3.c`, `sqlite3.h`, `sqlite3ext.h`)
is replaced with the unmodified upstream 3.51.3 release:

- Source: https://www.sqlite.org/2026/sqlite-amalgamation-3510300.zip
- ZIP SHA-256: `acb1e6f5d832484bf6d32b681e858c38add8b2acdfd42ac5df24b8afb46552b4`
- `sqlite3.c` SHA3-256: `32d5424f97e0a7fc5ed2f6335afbb58be4e0298bd7117a34e39d345ff13d859e`
- Release: https://www.sqlite.org/releaselog/3_51_3.html

The C source hash was checked against the official release page. This release
fixes the WAL-reset race described at https://www.sqlite.org/wal.html#walresetbug,
relevant to concurrent write/checkpoint workers. Earlier MemWeft builds bundled
SQLite 3.45.0. Passing short integrity tests did not establish absence of that race.

Existing bundled Rust bindings retain the compatible API subset and have their
version/source-ID constants updated. New SQLite C APIs are not used by MemWeft.
SQLite itself is public domain; see its source headers. SQLCipher files/features
are unchanged and are not the database engine used or validated by this project.

This source pin keeps offline builds reproducible. It should eventually be
replaced by compatible upstream rusqlite/r2d2_sqlite releases with a fixed bundled
SQLite; do not silently replace these sources with an older system SQLite.
