# Onboarding — from clone to running

This project separates **code** from **environment data**. Git carries the code and the
test suite's own fixtures; it does **not** carry the database, the input workbook, the
service/addressing YAML, or the compliance reference files. Those live on each server and
travel as a downloadable ZIP (see [ADR 0001](adr/0001-code-vs-environment-data.md)).

All of that data — and every writable output — lives under a single **data root**,
resolved from `FIRESTARTER_DATA` (default `<repo>/data`). The repository itself holds no
data. A deployment points `FIRESTARTER_DATA` at an external, project-namespaced directory
(`<data-root>`, or `/data` in a container); a plain checkout just uses
`<repo>/data`. See [ADR 0001](adr/0001-code-vs-environment-data.md) and the
`app.domain.file_locations` registry.

The practical consequence, and the thing that surprises everyone once:

> **A fresh clone has no data. The test suite passes anyway; the pipeline and the
> dashboard do not, until you seed data.**

---

## 1. Environment

Python 3.12.

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows
# source .venv/bin/activate   # Linux/macOS

pip install -e ".[dev]"
```

Verify:

```bash
pytest -q
```

This should pass with **zero data files on disk** — the suite builds its own in-memory
database and owns its fixtures under `tests/`. If it fails on a clean clone, that is a bug
in the test isolation, not a missing-data problem.

## 2. Get the data

Everything below is gitignored and must be obtained from a running environment. The paths
are **relative to the data root** (`FIRESTARTER_DATA`, default `<repo>/data`):

| What | Path (under the data root) |
|---|---|
| Database | `app.db` |
| Input workbook | `topology.xlsx` |
| Service definitions | `services/definitions/*_def.yaml` |
| Addressing policies | `services/addressing/*.yaml` |
| Compliance reference configs | `compliance/extra/*.cfg` |
| Compliance ignore rules | `compliance/ignore/*.yaml` |
| Per-role remediation specs | `compliance/remediation/*.yaml` |
| BOF definitions | `bof/definitions/*.yaml` |

**Option A — download a bundle from a running dashboard** (the normal route):

1. Open the dashboard → **Data → Data bundle**.
2. **Download bundle** — a ZIP of that server's complete data set, plus a `manifest.json`
   recording the source host, environment label, database schema revision, and a SHA-256
   per file.
3. Unzip it **into the data root**. The ZIP's paths are data-root-relative (flat — no
   `app/` prefix), so it unpacks straight onto the layout above:

```bash
unzip firestarter-data-<env>-<timestamp>.zip -d <data-root>   # e.g. ./data
```

There is deliberately **no upload** in the dashboard: loading a bundle is a filesystem
step, so nothing can overwrite a live database through a browser.

**Option B — seed a git worktree from another install's data root** (day-to-day development):

```bash
cd <worktree-path>
python -m app.data_bundle.seed_worktree <source-data-root>
```

This copies the same file set the download would, driven by the same spec, into this
checkout's data root (`<worktree>/data`), so the two can't drift.

### Which data will I get?

Whatever the server you took it from has. The test server holds lab values; the production
server holds production values. **There is no lab-vs-production distinction in the code** —
no filename convention encodes environment. `Base.yaml` on the test server and `Base.yaml`
on production are simply different files on different machines.

## 3. Database schema

The bundle's `manifest.json` records the Alembic revision its database is at. If the code
is newer than the data, bring the schema up:

```bash
alembic upgrade head
```

Migrations live in `migrations/` (`alembic.ini` sets `script_location = migrations`). The
directory is deliberately **not** named `alembic/` — a directory of that name at the repo
root shadows the installed `alembic` package under `python -m alembic`.

## 4. Run it

```bash
python main.py                              # full pipeline (needs data)
python app/compliance/compliance_snapshot.py   # compliance run (needs data)
```

The dashboard is a Flask app under `app/web`. Set `FIRESTARTER_ENV` on real servers
(`production`, `test`, …) so downloaded bundles are labelled with their origin; it defaults
to `unknown`.

---

## Windows notes

Two hazards have bitten Windows users before.

### Never `rm` a path inside `.venv`

A git worktree's `.venv` may be a Windows **junction** (`mklink /J`) pointing at the main
repo's `.venv`. Git Bash / MSYS2 `rm` **follows the junction** and empties the *target* —
i.e. it destroys the real `.venv` in the main repo. This has happened; there is no
recycle-bin recovery.

- Never use `rm` against a path that is, or sits inside, `.venv`, `site-packages`, or
  `node_modules`.
- The only safe removal of a junction is `cmd //c "rmdir <path>"` — `rmdir` unlinks the
  reparse point without touching the target.
- Verify what you have first: `fsutil reparsepoint query <path>` reports whether it is a
  real reparse point.

`.claude/settings.json` blocks these at the tooling layer, but the rule applies to anything
you run by hand.

### `app.db` is locked while the dashboard runs

A running Flask dashboard holds an open SQLite connection. On Windows that blocks git from
rewriting or deleting the file — `git checkout`/`git merge` fail with
`unable to unlink old 'app.db': Invalid argument`. **Stop the dashboard** before any git
operation that touches `app.db`.

---

## Worktree teardown

This part is not Windows-specific.

1. `cd` out of the worktree.
2. `git worktree remove <worktree>` — on Linux/macOS this deletes the whole
   worktree directory, `.venv` included; no separate step is needed. **On
   Windows**, if `.venv` is a junction it must be unlinked *first* with
   `cmd //c "rmdir <worktree>/.venv"` (never `rm` — see the Windows notes above),
   before running this.
3. `git branch -d <branch>`
4. `git worktree prune`

---

## Applying a database schema change

- **Adding a new table** is purely additive: `ensure_schema()`'s `create_all` (already wired
  into the web bootstrap) adds it without dropping anything, then a `seed_*` call populates
  it. No `wipe_db()`, no full rebuild.
- Reserve `wipe_db()` (drop_all + create_all + full pipeline rebuild) for changes to
  *existing* columns, or when you deliberately want a clean rebuild.
- Column-level changes go through Alembic (`migrations/`).

Because the build order is deterministic, a rebuild reproduces the same configuration —
which is what the compliance snapshot verifies.
