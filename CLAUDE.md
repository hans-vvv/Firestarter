# Firestarter — Project Context

## Overview
A Network Configuration Builder (~18k LOC) that computes deterministic, vendor-specific
device configurations from a persisted topology and renders them using Jinja2 templates.
Target platform is Nokia SR OS with MD-CLI.

This repository is the **public demo** of a production tool. It ships with a small
synthetic topology and no real devices: the Nornir/Netmiko automation code is kept for
reference, but every action that would push to or fetch from a device is disabled.

Demo topology vocabulary: router roles `rr`, `core`, `pe` (PE pairs and half-open
rings terminating on `core1.<site>`), one CE per PE site (role `switch`). Workbook
sheets: Devices, DistDevices (PEs), Role, Cables, Site, PrefixPoolTypes, PrefixPools,
ResourcePools, HalfOpenRings, CEs. Services: `vprn` (VLAN 20, also the CE-management
VPRN via `subnet_info.ce_mgmt: true`) and `evpn_vpls` (VLAN 30).

## Tech Stack
- Python, SQLAlchemy (SQLite), Alembic, Jinja2
- Nornir, Netmiko (present in the codebase; the demo runs without devices)
- Pydantic (YAML files) and custom scripting (Excel file) for validation
- Flask, htmx 2 and Bootstrap 5 for the dashboard

## Code Style Principle
Prefer flat, direct code over abstraction. Classes only when state genuinely needs to be
carried. No indirection for its own sake — if a plain function works, use it.

## Code Style Conventions
- Prefer keyword-only arguments (`*`) on all non-trivial methods
- `require()` utility for fail-fast DB lookups — see `app/utils`
- Typed SQLAlchemy models using `Mapped[]` throughout
- Thorough docstrings describing why, not just what, if functionality is non-trivial
- `from __future__ import annotations` at top of every module, except empty `__init__.py` files
- SQLAlchemy 2.x query style: `session.execute(select(...)).scalars().all()`

## Core Principles
- **Idempotency** — all operations are safe to re-run
- **Deterministic output** — same inputs always produce same outputs

## Architecture (pipeline order)
1. Excel input → validation → actions blob
2. Job Executor → Job Handlers → Topology Builders
3. IP/Resource allocation (YAML-driven policies)
4. Service Orchestrator → Service Builder → Feature Handlers
5. Context Layer → Jinja2 rendering → Printer

## Additional Modules
- Compliance module: `app/compliance`
- Web module: `app/web` (utilities and session wiring in `app/web/utils.py`)

## Further Reading
- `README.md` — what the demo is and how to start it
- `docs/ARCHITECTURE.md` — what the system is
- `docs/ADDING_A_NEW_SERVICE.md` — how to add a service type
- `docs/ONBOARDING.md` — clone → running; what a fresh clone does and does not contain
- `docs/adr/` — **why** decisions were made. Read before proposing to change one:
  - `0001` code vs environment data (why only code is tracked; the demo's exception)
  - `0002` minimal YAML validation (why there is no full schema)
  - `0003` where logic lives (YAML vs template vs handler)
  - `0004` compliance remediation

## Development Workflow
```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/alembic upgrade head                 # create / migrate the SQLite database
.venv/bin/python -m pytest -q                  # tests own their fixtures; no data needed
.venv/bin/ruff check app tests                 # lint
.venv/bin/pyright app                          # type check
```

- All environment data (database, `topology.xlsx`, service/addressing YAML, compliance
  inputs, rendered outputs) lives under one data root (`FIRESTARTER_DATA`, default
  `<repo>/data`). The test suite does not depend on it.
- In this demo repo the data root's *inputs* (`topology.xlsx`, service/addressing YAML,
  compliance inputs, `simulation/drift.yaml`) are tracked in git; everything generated
  (database, rendered configs, backups, inventory, snapshots, reports, logs) is gitignored
  and rebuilt by `scripts/bootstrap_demo.py`. In production nothing under it is tracked
  (ADR 0001).
- Column-level schema changes go through Alembic (`migrations/`); new tables are
  additive via `ensure_schema()`.
- `app/compliance/compliance_snapshot.py` (then `--diff`) captures and compares the
  rendered configuration of the whole topology, for checking that a change does not
  alter rendered configs unintentionally.
- Never run `git clean -x` — the `-x` flag deletes ignored files, which is all generated
  data under the data root. Permissions and deny rules live in `.claude/settings.json`.
