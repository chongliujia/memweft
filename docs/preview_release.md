# 0.2.0-alpha.1 developer preview

The Cargo and npm version is `0.2.0-alpha.1`; Python uses the equivalent PEP 440
version `0.2.0a1`. This preview packages the SQLite memory engine and SDKs.
It is not a production support or availability commitment.

## Changes and compatibility

- Python `required_fact_keys` and Node `requiredFactKeys` select explicit task
  dependencies alongside lexical retrieval, with `report.requirements`
  diagnostics for included, missing and budget-excluded keys.
- Required keys obey memory scope, pool access, update, forgetting and context
  limits. They do not prove that the supplied facts are correct or fresh.
- Empty-query truncation now reports `unranked_fact_limit` when the fact count
  limit excludes candidates.
- The repository includes Kimi examples, frozen comparison tasks and an
  application-level workflow validator. These examples and evaluations are
  **not bundled in the Python wheel or npm tarball**; use the same source commit
  to run them. Model calls are opt-in and may incur a charge.

The SQLite recall schema remains v3; this release adds no further migration.
Read the [upgrade guide](upgrade_v3.md) before opening older databases.
Python and Node options are additive. Rust code using a `ContextOptions` struct
literal must add `required_fact_keys` or use `..Default::default()`.
Context reports gain fields, so strict downstream schemas may need updating.

## Build and installation evidence

The [workflow](../.github/workflows/verify.yml) configures the following matrix.
Configuration alone is not evidence that a target passed: inspect the completed
run for the **exact source commit**, including every matrix job, before shipping.

| Target runner | Architecture | Python wheels | Node tarball test runtime |
|---|---|---|---|
| `ubuntu-24.04` | x86_64, glibc | 3.10, 3.11, 3.12 | Node 20 |
| `macos-15` | arm64 | 3.10, 3.11, 3.12 | Node 20 |
| `windows-2025` | x86_64 | 3.10, 3.11, 3.12 | Node 20 |

Python wheels must match the interpreter and platform tags in their filenames.
Node tarballs contain one host-specific native addon: their common filename
does not identify the platform. Keep each tarball with its original CI artifact
directory and manifest. Other architectures, musl Linux and broader OS/runtime
compatibility are not covered by this matrix. Packages are not yet published to
PyPI or npm registries.

Each CI artifact includes:

- The wheel, or the npm tarball and native CLI.
- `build-record.json`: source commit, clean-tree observation before and after
  the build, workflow digest, run identity when available, and artifact hashes.
- `package-target.json`: package versions read from the archives, host/runtime,
  SHA-256 hashes and results of installation outside the repository. Its
  `verification_source` describes the verifier checkout; artifact build source
  comes from the separately matched build record.

These records make a build auditable; they are not signed attestations or a
reproducible-build guarantee. The CLI is built by CI but is not part of the SDK
installation smoke test. CI artifacts follow GitHub's retention policy.

```bash
python -m pip install /path/to/memweft-0.2.0a1-<matching-tags>.whl
npm install /path/to/memweft-0.2.0-alpha.1.tgz
```

The isolated smoke tests cover native import, installed version, exact required
keys outside the ordinary candidate window, completeness diagnostics, token and
fact limits, tenant/user/agent isolation, same-key updates, reopening, and
forgetting that survives reopening. They also inspect package contents and
license inclusion. CI runs SDK integration and Rust tests; Linux Python 3.12
also runs the offline evaluation suite. PostgreSQL/MySQL integration requires
separate configured test databases and is not an acceptance claim of this preview.

## Interpretation of earlier evaluations

The September reports retain the package versions, source/native hashes and
GitHub state observed when they were produced. They describe historical
experiments, not the build identity or cross-platform acceptance of this preview.
The [required-facts evaluation](required_facts.md) uses a small frozen task set;
it supports further trials, not a general accuracy or production-readiness claim.
The next product milestone is a bounded external workflow pilot with measured
acceptance, rejection reasons and operating cost.
