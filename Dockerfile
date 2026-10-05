# syntax=docker/dockerfile:1
#
# Two-stage build for the `dltrack-server` distribution (see CLAUDE.md's "Packaging" section):
# stage 1 resolves and builds the project with uv, including its own build-time tooling (hatchling,
# the workspace's `hatch_build.py` hooks); stage 2 is a from-scratch runtime image that only ever
# sees the finished virtual environment, never uv, a C compiler, or the two packages' source tree
# outside what got installed into `site-packages`.

FROM python:3.12-slim-bookworm AS builder
# Installed from PyPI (pinned to this repo's own uv version) rather than copied from
# ghcr.io/astral-sh/uv: that image isn't reachable from every build environment (registries other
# than the index PyPI itself resolves through are sometimes blocked), and pip install is just as
# supported a way to get uv into the builder stage.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --root-user-action=ignore uv==0.9.21

ENV UV_PYTHON_DOWNLOADS=0 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app

# Dependency-only layer: resolve and install everything `dltrack-server` depends on, without
# building the workspace members themselves yet, so this layer only invalidates when the lockfile
# or one of the three `pyproject.toml`s actually changes -- not on every source edit. uv needs each
# workspace member's `pyproject.toml` on disk to know the workspace layout even with
# `--no-install-workspace`, but not its source or `hatch_build.py` hook.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=client/pyproject.toml,target=client/pyproject.toml \
    --mount=type=bind,source=server/pyproject.toml,target=server/pyproject.toml \
    uv sync --frozen --no-install-workspace --no-dev --no-editable \
        --package dltrack-server --extra postgres --extra s3

# Now the real source tree (both packages' code, since `dltrack-server` is built from a
# `force_include` of `../dltrack`'s server-owned subset -- see `server/hatch_build.py`) and the
# project itself, built non-editable so the wheel's files land in `site-packages` instead of a
# `.pth` pointing back at this stage's `/app`.
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package dltrack-server --extra postgres --extra s3

FROM python:3.12-slim-bookworm
RUN groupadd --system dltrack && useradd --system --gid dltrack --create-home dltrack

COPY --from=builder --chown=dltrack:dltrack /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Postgres (metadata) + local disk (artifacts) deployment, password sign-in -- see
# `dltrack/scripts/docker_deployment.py` for the exact plugin composition `--plugins` resolves
# below, and docs/docker.md to swap the artifact store for S3 instead (the image already ships the
# `s3` extra, so that needs no rebuild). Configure it entirely with env vars:
#   DLTRACK_POSTGRES_HOST / _PORT / _DATABASE / _USER / _PASSWORD  (dltrack/plugins/data_stores/postgres.py)
#   DLTRACK_ARTIFACT_STORE_LOCATION  -- where blobs land inside the container; defaults to /data/artifacts
#   (set below), so a volume mounted at /data is the only thing that needs to persist besides
#   Postgres itself.
#   DLTRACK_SECRET_KEY  -- required by the password auth plugin this image runs; a missing one is a
#   startup error, not a silent fall-through to anonymous access.
# See docs/plugins/storage.md and docs/plugins/auth.md for the full variable lists. Create the
# first admin with:
#   docker run --rm -it --env-file .env <image> users set-password <name> --admin \
#     --plugins dltrack.scripts.docker_deployment:PLUGINS
ENV DLTRACK_ARTIFACT_STORE_LOCATION=/data/artifacts
# `FSBlobs` creates its own leaf directory on first use, but never the volume mount point above it
# -- `/data` has to exist (and be writable by `dltrack`) before a volume is mounted over it at
# `docker run` time.
RUN mkdir -p /data && chown dltrack:dltrack /data
VOLUME ["/data"]

USER dltrack
WORKDIR /home/dltrack
EXPOSE 8050

# `curl`/`wget` aren't in this slim image and installing either just for this would pull in apt
# machinery this image otherwise never needs -- the base image's own python (already on PATH) can
# hit the liveness route with nothing extra installed.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8050/healthz', timeout=2).status == 200 else 1)"

ENTRYPOINT ["dltrack"]
CMD ["serve", "custom", "--plugins", "dltrack.scripts.docker_deployment:PLUGINS", "--host", "0.0.0.0"]
