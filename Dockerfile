# Firestarter demo image.
#
#   docker run -p 8080:8080 ghcr.io/hans-vvv/firestarter   → http://localhost:8080
#   docker compose up --build                              → same, built from this checkout
#   docker build --target test -t firestarter-test .       → image that runs the test suite
#
# Code and data stay separate (ADR 0001):
#   /opt/firestarter            the code (read-only, owned by root)
#   /opt/firestarter/demo-seed  the demo INPUTS from data/ (topology.xlsx, YAML,
#                               compliance rules, drift.yaml): a read-only starter kit
#   /data                       the data root (FIRESTARTER_DATA): database, rendered
#                               configs, backups, reports, logs. Mount a volume here.
# On first start the entrypoint copies the starter kit into an empty /data and
# builds the demo there (scripts/bootstrap_demo.py). After that only /data is used.

# ---------------------------------------------------------------------------
# Stage "base": Python + dependencies + Firestarter code + demo starter kit
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS base

LABEL org.opencontainers.image.title="Firestarter demo" \
      org.opencontainers.image.description="Network configuration builder (Nokia SR OS) with a synthetic demo network" \
      org.opencontainers.image.source="https://github.com/hans-vvv/Firestarter" \
      org.opencontainers.image.licenses="MIT"

# Python behaviour inside a container:
#  - no .pyc files written next to the code
#  - log lines appear immediately in "docker logs" (no buffering)
#  - "app" is importable from /opt/firestarter
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/opt/firestarter

# Unprivileged service account. It owns /data (the data root), NOT the code.
RUN useradd --uid 10001 --create-home --home-dir /var/lib/firestarter \
            --shell /usr/sbin/nologin firestarter \
 && mkdir -p /data \
 && chown firestarter:firestarter /data

WORKDIR /opt/firestarter

# Dependencies FIRST: this layer is only rebuilt when pyproject.toml changes.
COPY pyproject.toml .
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" \
        > /tmp/requirements.txt \
 && pip install -r /tmp/requirements.txt gunicorn \
 && rm /tmp/requirements.txt

# Code LAST: changes often, so it gets the top layers.
# Owned by root: the app can read its code, but never change it.
COPY app/ app/
COPY migrations/ migrations/
COPY scripts/bootstrap_demo.py scripts/
COPY alembic.ini run.py ./
COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/

# The demo starter kit: inputs only (see .dockerignore), never generated data.
COPY data/topology.xlsx demo-seed/
COPY data/services/ demo-seed/services/
COPY data/compliance/ demo-seed/compliance/
COPY data/simulation/ demo-seed/simulation/

# Whatever the file modes on the build machine: everyone may read the code
# and the starter kit (the app runs as an unprivileged user), nobody else may change them.
RUN chmod -R a+rX,go-w /opt/firestarter
USER firestarter
# ONE variable puts all environment data and output on the volume.
# FIRESTARTER_DEMO=1: the documented admin / changeme login stays usable.
ENV FIRESTARTER_DATA=/data \
    FIRESTARTER_DEMO=1
VOLUME ["/data"]
EXPOSE 8080

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:8080", \
     "--workers", "1", "--threads", "4", \
     "--timeout", "900", "--graceful-timeout", "30", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "app.web:create_app()"]

# ---------------------------------------------------------------------------
# Stage "test": base + the dev tools from pyproject.toml + the test suite.
# Built only with --target test. Runs the suite, not the dashboard.
# ---------------------------------------------------------------------------
FROM base AS test
USER root
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['optional-dependencies']['dev']))" \
        > /tmp/requirements-dev.txt \
 && pip install -r /tmp/requirements-dev.txt \
 && rm /tmp/requirements-dev.txt
COPY tests/ tests/
RUN chmod -R a+rX,go-w tests
USER firestarter
ENTRYPOINT []
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

# ---------------------------------------------------------------------------
# Stage "runtime": the image you run and publish. It is the LAST stage, so it
# is what "docker build" produces by default. No tests, no pytest.
# ---------------------------------------------------------------------------
FROM base AS runtime
