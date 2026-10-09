# 0001 — Separate code from environment data

**Status:** Accepted (2026-07-20)

## Context

The project runs on more than one server: a test environment with a testbed, and a
production environment. Historically git tracked everything — including `app.db`, the input
workbook `app/topology.xlsx`, the service-definition YAML, the addressing policies, and the
compliance reference/ignore files.

That is fine with one machine and one person. With two environments it actively hurts,
because **those files legitimately differ per server**:

- The database reflects each environment's real topology and allocations.
- The service YAML has the same *structure* everywhere but different *values* — ASNs,
  hostnames, tenant labels.
- A compliance ignore rule such as `Base.yaml` may or may not be identical between test and
  production.

Git tracking them forces one version onto every environment, and merging code drags one
environment's data along with it. The workflow that actually happens is: develop code and
YAML structure in test against the testbed, then run the same code in production against
production values. Feeding production values in is a *data* activity, not a development
activity.

## Decision

**Git carries code and the test suite's own fixtures. Each server carries one complete,
independent data set. Data moves between machines as an explicit, downloadable ZIP bundle.**

Untracked (gitignored, present on disk, travels via the bundle):

- `app.db`, `app/topology.xlsx`
- **all** `app/services/services_definitions/*_def.yaml`
- **all** `app/services/service_handling/addressing_definitions/*.yaml`
- `app/compliance/extra/*.cfg`, `app/compliance/ignore/*.yaml`
- `app/backups/` — device config backups, i.e. live production configuration

Only directory markers stay tracked (`compliance/ignore/__init__.py`,
`compliance/extra/.gitkeep`).

Three properties of the decision are deliberate:

1. **No lab-vs-production distinction anywhere in the code.** No filename convention encodes
   environment. Which values a bundle carries depends only on *which server you downloaded
   from*. An earlier iteration did split on `*_lab_def.yaml` vs `*_def.yaml`; it was removed
   because it duplicated, in the code, information that the server already implies.
2. **Download only — there is no upload.** Loading a bundle is `unzip` at the repository
   root (the manifest records each file's relative path). A web upload would only be needed
   to push data into a machine you cannot reach with a shell, which is exactly the dangerous
   direction — overwriting a live database through a browser. Dropping it removed the entire
   restore surface: untrusted-archive validation, path-traversal guarding, atomic replace,
   engine disposal, soft-delete and rollback.
3. **The test suite depends on none of it.** Tests own their fixtures under `tests/`.

## Consequences

**Good**

- Environments are genuinely independent; merging code never moves data.
- The bundle is a complete, checksummed snapshot of an environment (manifest records source
  host, environment label, Alembic revision, SHA-256 and size per file).
- The ignore rules got *simpler*: an earlier lab/production whitelist (ignore-all plus five
  negations, with a footgun where a newly added shared file would be silently ignored)
  collapsed to plain per-directory lines.

**Costs**

- **A fresh clone cannot run the pipeline or the dashboard until data is seeded.** This is
  the main ergonomic cost of this decision. A fresh
  worktree needs `python -m app.data_bundle.seed_worktree <main-repo-root>` before the
  compliance baseline can run.
- The YAML no longer has git history. Its values are data; review and rollback for them are
  whatever the environment provides, not `git log`.
- Nothing in CI validates the *real* definitions any more. A malformed production YAML
  surfaces at pipeline time, not in the test suite. Accepted knowingly — see below.

## Assumptions that were tested and turned out false

Recorded because they were each stated confidently, and each one would have led to a worse
design. All were settled by removing files and running the suite.

1. **"The lab YAML is the test corpus — 15 test files depend on it."** False. Removing all
   15 service `*_lab_def.yaml` changes nothing (full suite passes). Those tests build their
   service data as inline synthetic fixtures — `tests/.../test_bgp.py` uses `asn: 65000`
   while the real lab definition says `65058`. The real file is never read.
2. **"The compliance snapshot needs the service definitions."** False. The Printer renders
   from the `computed` blob already stored in `app.db`, not from the YAML. Service
   definitions are a **pipeline-time input only** — they matter when services are recomputed
   into the database, not when configuration is rendered.
3. **"The compliance `extra`/`ignore` files are test dependencies."** False. The compliance
   tests monkeypatch `EXTRA_DIR`/`IGNORE_DIR` to temporary directories and build their own
   fixtures.

The single genuine coupling was **`addressing_lab_def.yaml`**: `AddressingPolicyResolver.install()`
globs the addressing directory, so every topology-seeding test needed at least one policy
(12 failures + 70 errors, "No addressing policies found"). That was resolved by giving the
suite its own fixture — see [ADR 0003](0003-where-logic-lives.md) and
`tests/fixtures/addressing/`.

**Verification that now guards this decision:** the full suite passes with *every* piece of
environment data absent — no YAML, no compliance configs, no ignore rules, no database. If
that ever stops being true, a test has silently reacquired a data dependency.

## Runtime counterpart: the data-root registry

The split above decides *what* is data; `app.domain.file_locations` decides *where* it lives
at runtime. Every data file and writable output directory is declared there, relative to a
single `DATA_ROOT` that defaults to `<repo>/data` and is overridden by the `FIRESTARTER_DATA`
environment variable. That one variable repoints all data and output at once — the seam that
lets a deployed instance use an external, project-namespaced root (`<data-root>`) or
a container keep code read-only in the image and mount mutable state on a `/data` volume. No
module resolves a data path from its own `__file__` or the process CWD; `app.data_bundle.spec`
derives its taxonomy from the same registry for the seed/bundle path.

The on-disk layout is **flat and code-free** (`<DATA_ROOT>/topology.xlsx`,
`<DATA_ROOT>/compliance/ignore/`, …) — the data root no longer shadows the `app/` package, so
the repository / image is pure code and the gitignore is a single `/data/` line. An existing
install on the old nested layout is moved onto this one with
`python -m app.data_bundle.migrate_layout <old-root> <data-root>`, which copies the old
`app/...` data across and leaves the old tree intact until you verify.

## The demo repository

The public demo deliberately deviates from the decision above. Its data is synthetic and
the same everywhere, and the *inputs* are the demo itself, so they are tracked under
`data/`: `topology.xlsx`, `services/{definitions,addressing}/*.yaml`,
`compliance/{extra,ignore,remediation}/*` and `simulation/drift.yaml`. Everything the
pipeline and the simulation *generate* (database, rendered artifacts, backups, inventory,
snapshots, reports, logs) stays gitignored and is rebuilt by `scripts/bootstrap_demo.py`.
The gitignore therefore lists those generated paths instead of a single `/data/` line.
The data root, the registry and the bundle mechanism are unchanged.

## Notes for future work

- ~~Once device backups are produced automatically, that backup directory should join the
  bundle.~~ **Done.** Nornir now writes `app/backups/latest/` on the application host and
  that directory is in the bundle spec, so a downloaded data set can be compliance-checked
  offline against whichever environment it came from. Only `latest/` travels; the
  timestamped archives beside it are local history.
- Do **not** add a lab-only filter to the compliance run. Being able to pull production data
  and check it is a goal, not a hazard.
