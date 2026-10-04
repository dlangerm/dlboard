# Docker

## What is this

The root [`Dockerfile`](../Dockerfile) builds the `dltrack-server` distribution into a container
image. It's a two-stage build — a `uv`-based builder stage that resolves and compiles the project,
and a slim runtime stage that only ever sees the finished virtual environment, never `uv`, a
compiler, or the source tree outside what got installed into `site-packages`.

The image's default deployment is **Postgres metadata + local-disk artifacts, with password
sign-in** — composed in
[`dltrack/scripts/docker_deployment.py`](../dltrack/scripts/docker_deployment.py):

```python
PLUGINS = [*POSTGRES_STORAGE, *PASSWORD_AUTH, *BUILTIN_BACKEND, *BUILTIN_CHARTS, themes.default]
```

This is the simplest deployment that's still a real, multi-person deployment rather than `dltrack
serve local`'s single-user setup: one Postgres database plus one volume for artifact blobs, nothing
else to run or pay for. The image also ships the `s3` extra, so pointing it at an S3-protocol store
instead needs no rebuild — see "Using an S3 artifact store instead" below.

## When you'd need this

You're running dltrack in Docker/Compose/Kubernetes/ECS rather than installing it directly with
`pip`/`uv tool install`. If you're running it as a plain process on a machine you control, see the
root [README](../README.md#running-it) instead — there's no reason to containerize just to get
`dltrack serve local` or `serve custom` running.

## Running it

It needs a reachable Postgres to actually start — that's not optional, since the default deployment
has no other metadata store to fall back to. Artifact storage defaults to a local directory inside
the container (`/data/artifacts`), so it only needs a volume, not another service.

To try it with no infrastructure of your own, [`docker-compose.yml`](../docker-compose.yml) at the
repo root is entirely self-contained:

```bash
docker compose up --build
docker compose exec dltrack \
  dltrack users set-password alice --admin --plugins dltrack.scripts.docker_deployment:PLUGINS
```

It builds the image and brings up a Postgres container alongside it — nothing to configure first.
Both of its volumes (`dltrack-postgres`, `dltrack-artifacts`) genuinely persist across `docker
compose down`/`up` (no `-v`); that was checked by actually restarting the stack and confirming a
project and an uploaded artifact were both still there afterward, not assumed from "it's a volume."
It's still a local trial, not a deployment you'd run as-is: `DLTRACK_SECRET_KEY` is a hardcoded
placeholder in the file, and `docker compose down -v` throws both volumes away on purpose.

Pointed at your own Postgres instead, override that same file's environment (or write your own
`docker-compose.yml`/Kubernetes manifest): point `POSTGRES_HOST` at your real database, set a real
`DLTRACK_SECRET_KEY` (`openssl rand -hex 32`), and either keep mounting a volume at `/data` for
artifacts or switch to S3 (below).

## Configuration

Every setting is an environment variable — nothing is passed on the container's command line.
These are the ones every deployment needs; the full lists (TLS, pooling, timeouts, ...) are in
[storage.md](plugins/storage.md) (`PostgresSettings`) and [auth.md](plugins/auth.md) (`PASSWORD_AUTH`'s
settings table).

| Variable | Required | |
|---|---|---|
| `POSTGRES_HOST` | yes | Also `_PORT` (5432), `_DATABASE` (`dltrack`), `_USER` (`dltrack`), `_PASSWORD`. |
| `ARTIFACT_STORE_LOCATION` | no | Where artifact blobs land inside the container. Defaults to `/data/artifacts` — mount a volume at `/data` to persist them. |
| `DLTRACK_SECRET_KEY` | yes | Signs session cookies for the password auth plugin this image runs. A missing one is a startup error, not a silent fall-through to anonymous access — generate one with `openssl rand -hex 32`. |
| `DLTRACK_ADMIN_USERS` | no | Comma-separated usernames made admin whenever they sign in — the alternative to creating the first admin by hand (below). |

Serve it over HTTPS in front of a reverse proxy — session cookies are `Secure` by default, which
most HTTP clients (browsers included, `localhost` sometimes excepted) silently refuse to send back
over a plain-HTTP connection, breaking sign-in rather than just weakening it. `docker-compose.yml`
sets `DLTRACK_SECURE_COOKIES=false` for exactly this reason, since it's plain HTTP by design — leave
it at its default (`true`) everywhere else (see [auth.md](plugins/auth.md) for rate-limiting
`/login` too).

## Creating the first admin

```bash
docker run --rm -it --env-file .env dltrack-server \
  users set-password alice --admin --plugins dltrack.scripts.docker_deployment:PLUGINS
```

`--env-file .env` needs the same `POSTGRES_*` variables as the running container, since this writes
to the same database. After that, admins can add more users and reset passwords from Admin → Users
in the app itself — see [auth.md](plugins/auth.md).

## Authenticating a training script

Password sign-in means `dltrack serve local`'s "no key needed" shortcut doesn't apply here — a
training script is a separate caller from your browser session, and it authenticates the way
[client.md](client.md) describes for any signed-in deployment:

1. Sign in at `http://localhost:8050` (or wherever you exposed it) with the user you created above.
2. Create an API token on that user's Account page.
3. Wherever `DLTrackLogger` runs, set `DLTRACK_API_KEY` to that token and point it at the server:

   ```python
   from dltrack.client.dltrack_logger import DLTrackLogger

   logger = DLTrackLogger(project_id=project.id, server_url="http://localhost:8050")
   ```

If the training script runs in its own container on the same Compose network, use the service name
instead of `localhost` — `server_url="http://dltrack:8050"`. The logger checks the key against
`/whoami` before training starts, so a missing or wrong `DLTRACK_API_KEY` fails immediately rather
than silently dropping every metric.

## Using an S3 artifact store instead

Local disk is the simplest default, not the only option — the image is already built with the `s3`
extra, so swapping `dltrack.plugins.POSTGRES_STORAGE` for `POSTGRES_S3_STORAGE` (postgres + S3 +
the artifact purge worker) needs no rebuild, just your own small module instead of
`docker_deployment.py` (dltrack deliberately doesn't ship a ready-made S3 "deployment" of its own —
see [storage.md](plugins/storage.md)):

```python
# mydeployment.py
from dltrack.plugins import BUILTIN_BACKEND, BUILTIN_CHARTS, PASSWORD_AUTH, POSTGRES_S3_STORAGE, themes

PLUGINS = [*POSTGRES_S3_STORAGE, *PASSWORD_AUTH, *BUILTIN_BACKEND, *BUILTIN_CHARTS, themes.default]
```

Mount it into the container and point `--plugins` at it instead:

```bash
docker run -v ./mydeployment.py:/app/mydeployment.py:ro -e PYTHONPATH=/app \
  -e POSTGRES_HOST=... -e S3_BUCKET=... -e DLTRACK_SECRET_KEY=... \
  dltrack-server serve custom --plugins mydeployment:PLUGINS --host 0.0.0.0
```

`S3_BUCKET` is required once you do this; `S3_ENDPOINT_URL`/`S3_ADDRESSING_STYLE=path` instead of
AWS for a self-hosted S3-protocol store (MinIO, VAST, ...). See [storage.md](plugins/storage.md) for
the full `S3Settings` list.

The `docker-compose.yml` at the repo root deliberately doesn't do this: a throwaway S3-protocol
emulator worth bundling for a zero-setup local trial turned out not to exist for free — LocalStack's
free tier doesn't actually persist state across a restart (checked, not assumed) and MinIO's own
images now require a Docker Hub login (see `dltrack/conftest.py`'s `s3_test_server`). Local disk
sidesteps both and is just as real a storage choice for a small deployment.

## Running a different plugin list

More generally, the image's `ENTRYPOINT`/`CMD` just runs `dltrack serve custom --plugins
dltrack.scripts.docker_deployment:PLUGINS --host 0.0.0.0` — override either to run your own
`PLUGINS` module (a different auth provider, a different theme, S3 as above) the same way:

```dockerfile
FROM dltrack-server
COPY mydeployment.py /app/mydeployment.py
ENV PYTHONPATH=/app
CMD ["serve", "custom", "--plugins", "mydeployment:PLUGINS", "--host", "0.0.0.0"]
```

See [plugins/overview.md](plugins/overview.md) for what a `PLUGINS` module needs, and
[storage.md](plugins/storage.md)/[auth.md](plugins/auth.md) for the built-in plugins it can compose.

## CI

`.github/workflows/ci.yml`'s `docker-build` job builds this image on every PR as a sanity check — it
never pushes it anywhere. Publishing an image to a registry isn't set up yet.
