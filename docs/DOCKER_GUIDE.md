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

### 2.3 First run – throw-away (no volume)

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

### 2.4 Second run – with a volume (data survives)

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

## 3. Publish to GitHub Container Registry (ghcr.io)

### 3.1 Tag a version – this starts the build on GitHub

```bash
git tag v0.1.0
git push origin v0.1.0
```

### 3.2 Watch the build

On GitHub: repo **hans-vvv/Firestarter** → tab **Actions** → run *Publish container image*.
It takes about 3–6 minutes: tests in the image → login → build → push.
A green check means the image is published as:

- `ghcr.io/hans-vvv/firestarter:0.1.0`
- `ghcr.io/hans-vvv/firestarter:latest`


### 3.3 Test as a reader – on the test VM

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo docker run --rm -p 8080:8080 ghcr.io/hans-vvv/firestarter
```

Open `http://<testvm-ip>:8080`, log in as admin / changeme.
If `docker run` asks for credentials or says *denied*, the package is still private (4.3).

---

## 4. Releasing a new version later

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
