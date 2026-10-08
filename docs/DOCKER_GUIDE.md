# Firestarter demo – build and publish the Docker image

Step-by-step, in the order you do it. Everything runs on the **dev VM** in
`/opt/firestarter` (VS Code Remote-SSH terminal), unless a step says otherwise.

End result: anyone can run the demo with one command:

```bash
docker run -p 8080:8080 ghcr.io/hans-vvv/firestarter
```

---

## 0. What is in this folder

| File | Goes to | What it does |
|---|---|---|
| `Dockerfile` | `/opt/firestarter/Dockerfile` | Recipe for the image: Python 3.12 + code + demo starter kit. Has a `test` stage (runs the suite) and a `runtime` stage (the image you publish). |
| `docker-entrypoint.sh` | `/opt/firestarter/docker-entrypoint.sh` | Runs at every container start. If `/data` is empty, it copies the starter kit in and builds the demo (`bootstrap_demo.py`). Otherwise it does nothing. |
| `docker-compose.yml` | `/opt/firestarter/docker-compose.yml` | Convenience: build + run with a named volume, port 8080. |
| `.dockerignore` | `/opt/firestarter/.dockerignore` | **Allowlist** of what may enter the image: code, migrations, `bootstrap_demo.py`, the demo inputs in `data/`, tests. Everything else stays out (venv, databases, zips, `.env`, generated output). Replaces the old one. |
| `publish-image.yml` | `/opt/firestarter/.github/workflows/publish-image.yml` | GitHub Actions: on a version tag, run the tests in the image, then build and push it to `ghcr.io`. |

Inside the image:

```
/opt/firestarter             code (read-only, owned by root)
/opt/firestarter/demo-seed   demo inputs: topology.xlsx, services/, compliance/, simulation/
/data                        data root (FIRESTARTER_DATA) – database, configs, backups, logs
```

---

## 1. Prepare the dev VM

### 1.1 Install Docker (once)

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
```

Then **log out and in again** so the group change takes effect. In VS Code:
Ctrl+Shift+P → *Remote: Close Remote Connection*, then connect again.

Check:

```bash
docker run --rm hello-world        # prints "Hello from Docker!"
docker compose version             # prints a version, e.g. v2.x
```

### 1.2 Put the files in the repo

1. In VS Code (connected to the VM), open the Explorer on `/opt/firestarter`.
2. In Windows Explorer, open `C:\Users\verke\Desktop\Firestarter_demo\docker-draft`.
3. Drag `Dockerfile`, `docker-entrypoint.sh`, `docker-compose.yml`, `.dockerignore`
   and `publish-image.yml` onto the `firestarter` root in VS Code. Overwrite `.dockerignore`.
   (Not `DOCKER_GUIDE.md` – that one is for you. If you want it in the repo, put it in `docs/`.)

Move the workflow to where GitHub looks for it, then check:

```bash
cd /opt/firestarter
mkdir -p .github/workflows
mv publish-image.yml .github/workflows/
ls -a Dockerfile docker-entrypoint.sh docker-compose.yml .dockerignore .github/workflows/
chmod +x docker-entrypoint.sh
```

### 1.3 Two `.gitignore` lines (if not done yet)

Make sure `.gitignore` has these next to `/data/app.db`:

```
/data/app.db-shm
/data/app.db-wal
/bundle/
```

---

## 2. Build and test locally

### 2.1 Run the test suite inside a container

```bash
docker build --target test -t firestarter-test .
docker run --rm firestarter-test
```

Expected: `NNNN passed`. This proves the image has everything the code needs.
The first build downloads Python 3.12 and the dependencies (a few minutes);
later builds reuse that and take seconds.

### 2.2 Build the real image

```bash
docker build -t firestarter-demo:local .
```

### 2.3 Check what is inside (leak check on the image)

```bash
# what got in
docker run --rm --entrypoint sh firestarter-demo:local -c 'ls -A /opt/firestarter; echo; find /opt/firestarter/demo-seed -type f'

# leak scan INSIDE the image – must print nothing
docker run --rm --entrypoint grep firestarter-demo:local -rniEI \
  'hanab|nms1|nms002|fc-ao|10\.8[567]\.|10\.79\.|lab-001|BT-|2a01:90a1|82\.158\.|pop_list|PMGR-|VPRN-INTERNET|HSI-FC|NLASD|-(odf|krf|eqn|nh)-0' \
  /opt/firestarter
```

Expected in `/opt/firestarter`: `alembic.ini app demo-seed migrations pyproject.toml run.py scripts`
– no `.venv`, no `tests`, no `data`, no `.git`.
`demo-seed` should hold 16 files: `topology.xlsx`, the YAMLs, `base.cfg`, `drift.yaml`.

### 2.4 First run – throw-away (no volume)

```bash
sudo ufw allow 8080/tcp          # only if ufw is active (sudo ufw status)
docker run --rm -p 8080:8080 firestarter-demo:local
```

You should see:

```
entrypoint: empty data root, building the demo from /opt/firestarter/demo-seed
alembic upgrade head   ...
...
run_compliance         20 compliant, 6 non-compliant, 1 fetch failure(s)
build_inventory        27 host(s), roles core, pe, rr
entrypoint: demo ready
[INFO] Listening at: http://0.0.0.0:8080
```

Open `http://<vm-ip>:8080` on your PC, log in as **admin / changeme**, click through
Devices, Jobs, Compliance, Admin.

Stop with **Ctrl+C**. Because of `--rm` the container *and* its data are gone;
the next run builds a fresh demo.

### 2.5 Second run – with a volume (data survives)

```bash
docker compose up -d --build     # build + start in the background
docker compose logs -f           # watch the first start; Ctrl+C stops watching, not the app
docker compose ps                # STATUS should become "healthy" after ~30 s
```

Test that data survives:

```bash
docker compose down              # removes the container, KEEPS the volume
docker compose up -d             # starts instantly: no "building the demo" in the logs
docker compose logs | grep entrypoint    # prints nothing = existing data was reused
```

Reset to a clean demo:

```bash
docker compose down -v           # -v deletes the volume too
docker compose up -d             # builds a fresh demo again
```

Stop it when done: `docker compose down`.

---

## 3. Commit the Docker files

```bash
cd /opt/firestarter
git add Dockerfile docker-entrypoint.sh docker-compose.yml .dockerignore .github .gitignore
git status --short               # only these files should be listed
git commit -m "Add Docker image, compose file and image publish workflow"
git push
```

---

## 4. Publish to GitHub Container Registry (ghcr.io)

### 4.1 Tag a version – this starts the build on GitHub

```bash
git tag v0.1.0
git push origin v0.1.0
```

### 4.2 Watch the build

On GitHub: repo **hans-vvv/Firestarter** → tab **Actions** → run *Publish container image*.
It takes about 3–6 minutes: tests in the image → login → build → push.
A green check means the image is published as:

- `ghcr.io/hans-vvv/firestarter:0.1.0`
- `ghcr.io/hans-vvv/firestarter:latest`

### 4.3 Make the image public (once)

New packages on ghcr.io start **private**.

1. github.com → your profile → tab **Packages** → **firestarter**.
2. Right side: **Package settings**.
3. Bottom, *Danger Zone*: **Change visibility** → **Public** → type the name to confirm.

On the package page, check that it shows *Connected to hans-vvv/Firestarter*
(the `org.opencontainers.image.source` label in the Dockerfile does that).

### 4.4 Test as a reader – on the test VM

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo docker run --rm -p 8080:8080 ghcr.io/hans-vvv/firestarter
```

No login, no Python, no clone. Open `http://<testvm-ip>:8080`, log in as admin / changeme.
If `docker run` asks for credentials or says *denied*, the package is still private (4.3).

---

## 5. Text for the README (reader quick start)

```markdown
### Run it with Docker (quickest)

    docker run -p 8080:8080 ghcr.io/hans-vvv/firestarter

Open http://localhost:8080 (or http://<server-ip>:8080) and log in as `admin` / `changeme`.
The first start builds the demo network (a few seconds).

To keep your changes between runs, give it a volume:

    docker run -d --name firestarter -p 8080:8080 -v firestarter-data:/data ghcr.io/hans-vvv/firestarter

Start over with a clean demo: `docker rm -f firestarter && docker volume rm firestarter-data`.

From a clone of this repository, `docker compose up -d --build` does the same with a local build.
```

---

## 6. Releasing a new version later

```bash
# after committing and pushing your changes
git tag v0.1.1
git push origin v0.1.1
```

GitHub builds and pushes `:0.1.1` and moves `:latest`.

Someone running with a volume upgrades with:

```bash
docker pull ghcr.io/hans-vvv/firestarter
docker rm -f firestarter
docker run -d --name firestarter -p 8080:8080 -v firestarter-data:/data ghcr.io/hans-vvv/firestarter
```

Note: the entrypoint only builds the demo when `/data` is **empty**. If a new version
changes the database schema, existing volumes need the migration once:

```bash
docker exec firestarter python scripts/bootstrap_demo.py    # idempotent: migrates, changes nothing else
```

(or simply delete the volume and get a fresh demo).

---

## 7. Settings you can pass

| Variable | Default | Meaning |
|---|---|---|
| `FIRESTARTER_ADMIN_PASSWORD` | `changeme` | Password of `admin`. Only used on the **first** start (when no user exists yet). |
| `SECRET_KEY` | demo value | Flask session key. Set a random value for anything that is not a throw-away demo. |
| `FIRESTARTER_PORT` (compose only) | `8080` | Port on the host. |
| `FIRESTARTER_SEED` | `/opt/firestarter/demo-seed` | Folder with inputs to build from. Mount your own topology/YAMLs and point this at them to build a different network. |

Example: `docker run -p 8080:8080 -e FIRESTARTER_ADMIN_PASSWORD=s3cret ghcr.io/hans-vvv/firestarter`

---

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `permission denied ... /var/run/docker.sock` | Not in the `docker` group yet: log out and in again (1.1), or prefix with `sudo`. |
| `failed to solve ... "/data/topology.xlsx": not found` | The demo inputs are not in `/opt/firestarter/data/`. Run `scripts/make_bundles.sh` (it syncs them from `/opt/firestarter-data`) or copy them. |
| `Permission denied` in the image check (2.3), or the first start fails copying the seed | Files on the build machine were readable only by their owner. The `RUN chmod -R a+rX,go-w /opt/firestarter` line in the Dockerfile fixes that inside the image; on the VM: `chmod -R a+rX app migrations scripts data tests`. |
| Image check (2.3) shows `.claude`, `docs`, `tests` or other extras in `/opt/firestarter` | `.dockerignore` is still the old one. Replace it with the one from this folder and rebuild. |
| `Bind for 0.0.0.0:8080 failed: port is already allocated` | Something else uses 8080. Use `-p 8081:8080` or `FIRESTARTER_PORT=8081 docker compose up -d`. |
| Page does not load from your PC | `docker ps` (is it running?), `sudo ufw allow 8080/tcp`, and use `http://` not `https://`. |
| Starts, but empty dashboard / no devices | `/data` was not empty (old volume) or the seed is missing. `docker compose down -v` and start again; check the `entrypoint:` lines in the logs. |
| `Permission denied` writing to `/data` | You mounted a host folder (`-v ./mydata:/data`). The app runs as uid 10001. Use a named volume, or `sudo chown -R 10001:10001 mydata`. |
| Actions run fails at *Log in* or *push* with `denied` / `403` | Repo → Settings → Actions → General → *Workflow permissions*: allow read and write. If the package already existed before: package → Package settings → *Manage Actions access* → add `hans-vvv/Firestarter` with **Write**. |
| Actions run fails at *Run the test suite* | Same failure you would get locally with `docker build --target test` (2.1). Fix, commit, push, then move the tag: `git tag -f v0.1.0 && git push -f origin v0.1.0`. |
| `docker run ghcr.io/...` asks for a login | Package still private (4.3). |

---

## 9. Good to know

- **The image never contains generated data.** The database, configs, backups and
  reports are made on first start, inside `/data`. Only the public demo *inputs*
  (the same files that are in git) travel in the image, as a starter kit.
- **Everything in a public image is public,** including older layers. The
  `.dockerignore` allowlist is what keeps the rest out; run the image leak scan
  (2.3) before every release tag.
- **Templates are found relative to the working directory** (`Path("app/services/templates/sros")`
  in `app/printing/printer.py`). In the image the working directory is always
  `/opt/firestarter`, so this is fine; outside Docker, always start from the repo
  folder. Worth making absolute some day.
