# Installing and running the Firestarter demo on a VM

Tested on Ubuntu 24.04 LTS (Python 3.12). Any Linux with Python 3.12+ works the same
way; the only system packages needed are `python3`, `python3-venv` and `git`.

The whole thing runs as an unprivileged user, needs no database server (SQLite) and
contacts no network devices (they are simulated). Give the VM 1 vCPU, 1 GB RAM and
2 GB of disk; that is plenty.

---

## 1. Prepare the VM

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git unzip
python3 --version        # must be 3.12 or newer
```

If your distribution ships an older Python (Ubuntu 22.04 has 3.10), install 3.12 from
the deadsnakes PPA first:

```bash
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt install -y python3.12 python3.12-venv
```

and use `python3.12` instead of `python3` in the commands below.

Create a user to run the demo as (optional but tidy):

```bash
sudo adduser --disabled-password --gecos "" firestarter
sudo su - firestarter
```

## 2. Get the code

Either clone the repository:

```bash
git clone https://github.com/<your-account>/firestarter-demo.git
cd firestarter-demo
```

or copy `firestarter-demo.zip` to the VM (`scp firestarter-demo.zip firestarter@<vm-ip>:`)
and unpack it:

```bash
unzip firestarter-demo.zip
cd firestarter-demo
```

## 3. Create the virtual environment and install

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"
```

This installs Flask, SQLAlchemy, Alembic, Jinja2, pandas/openpyxl, Nornir/Netmiko (kept for
reference, never used by the demo) and the dev tools (pytest, ruff, pyright).

Optional sanity check — the test suite needs no data and takes about a minute:

```bash
.venv/bin/python -m pytest -q
```

## 4. Build the demo data

The repository tracks the demo *inputs* under `data/` (topology workbook, service and
addressing YAML, compliance inputs, simulated-device drift). One script builds everything
else from them:

```bash
FIRESTARTER_DEMO=1 .venv/bin/python scripts/bootstrap_demo.py
```

You should see nine steps, each ending in a timing, finishing with:

```
run_pipeline            job do_all_...                      
activate devices        27 device(s) set active
render_all_to_disk      27 config(s) rendered
run_simulated_backup    26/27 simulated device(s) fetched, unreachable: pe1.Site19
run_compliance          20 compliant, 6 non-compliant, 1 fetch failure(s)
build_inventory         27 host(s), roles core, pe, rr
```

The script is idempotent: run it again whenever you want to reset the demo to a known
state (it does not touch the admin account).

What the two environment variables do:

| Variable | Effect |
|---|---|
| `FIRESTARTER_DEMO=1` | Seeds the `admin` account without forcing a password change on first login. Leave it unset on anything that is not a throw-away demo. |
| `FIRESTARTER_ADMIN_PASSWORD` | Bootstrap password for `admin` (default `changeme`). Only read when the account is first created. |
| `FIRESTARTER_DATA` | Data root (default `<repo>/data`). Set it to keep the data outside the checkout, e.g. `/srv/firestarter-data`; copy the tracked `data/` directory there first. |

## 5. Run the dashboard (quick way)

```bash
FIRESTARTER_DEMO=1 .venv/bin/flask --app run run --host 0.0.0.0 --port 5000
```

Open `http://<vm-ip>:5000` in a browser and log in as `admin` / `changeme`. Open the VM
firewall for port 5000 if needed (`sudo ufw allow 5000/tcp`). The browser needs internet
access for the Bootstrap CSS/JS that the pages load from a CDN; the application itself
makes no outbound connections.

`python run.py` also works but binds to `127.0.0.1` only, so it is reachable just from
the VM itself (or through an SSH tunnel: `ssh -L 5000:127.0.0.1:5000 firestarter@<vm-ip>`).

## 6. Run it as a service (recommended for a VM that stays up)

Install gunicorn into the venv and create a systemd unit.

```bash
.venv/bin/pip install gunicorn
```

`/etc/systemd/system/firestarter.service` (as root):

```ini
[Unit]
Description=Firestarter demo dashboard
After=network.target

[Service]
User=firestarter
WorkingDirectory=/home/firestarter/firestarter-demo
Environment=FIRESTARTER_DEMO=1
Environment=SECRET_KEY=change-me-to-something-random
ExecStart=/home/firestarter/firestarter-demo/.venv/bin/gunicorn --workers 2 --bind 0.0.0.0:5000 run:app
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now firestarter
sudo systemctl status firestarter
journalctl -u firestarter -f        # live log
```

`SECRET_KEY` signs the login cookie; set it to any long random string
(`python3 -c "import secrets; print(secrets.token_hex(32))"`).

## 7. A five-minute tour once it runs

1. **Overview** — device and job counts.
2. **Devices → Device list** — 27 SR OS routers (2 RR, 8 core, 17 PE) and 11 access
   switches. Select a router to see its rendered configuration.
3. **Data → Excel data** — the topology workbook; **Service data** — the YAML
   definitions (editable in the browser); **Run data pipeline** — recompute everything.
4. **Devices → Latest backups** — the simulated device backups (one device,
   `pe1.Site19`, is deliberately unreachable).
5. **Compliance → Compliance** — press *Run Compliance*: 6 devices show drift
   (missing BGP neighbour, wrong LAG description, rogue static route, extra local user,
   missing VPLS SAP, changed MTU). Expand a row for the exact lines.
6. **Compliance remediations** — *Show remediation candidates*: the lines the role
   specs would fix. Pushing is disabled in the demo.
7. **Admin** — user accounts and roles.

To change what the compliance page finds, edit `data/simulation/drift.yaml` and press
*Run Compliance* again (with *Fetch backups from simulated devices* ticked).

## 8. Updating

```bash
cd ~/firestarter-demo
git pull                                   # or unzip a newer archive over it
.venv/bin/pip install -e ".[dev]"
FIRESTARTER_DEMO=1 .venv/bin/python scripts/bootstrap_demo.py   # applies migrations, rebuilds data
sudo systemctl restart firestarter
```

## Troubleshooting

- `ModuleNotFoundError: app` — you are not in the repo directory or forgot
  `pip install -e`.
- `no such table` — run `scripts/bootstrap_demo.py` (it runs the Alembic migrations and
  creates the tables).
- Pages render without styling — the browser cannot reach `cdn.jsdelivr.net`.
- Login fails with `changeme` — the admin was created earlier with another password, or
  without `FIRESTARTER_DEMO=1` and you are being redirected to the change-password page.
  Delete `data/app.db` and run the bootstrap again to start over.
- Port 5000 refused from outside — bound to `127.0.0.1` (use `--host 0.0.0.0` or the
  systemd unit) or the firewall is closed.
