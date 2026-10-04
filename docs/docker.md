# Docker

## What is this

The root [`Dockerfile`](../Dockerfile) builds the `dltrack-server` distribution into a container
image. It's a two-stage build — a `uv`-based builder stage that resolves and compiles the project,
and a slim runtime stage that only ever sees the finished virtual environment, never `uv`, a
compiler, or the source tree outside what got installed into `site-packages`.

The image's default deployment is **Postgres + S3, with password sign-in** — composed in
[`dltrack/scripts/docker_deployment.py`](../dltrack/scripts/docker_deployment.py):

```python
PLUGINS = [*POSTGRES_S3_STORAGE, *PASSWORD_AUTH, *BUILTIN_BACKEND, *BUILTIN_CHARTS, themes.default]
```

Unlike `dltrack serve local`'s sqlite+filesystem defaults, both stores here are external services,
so the container itself holds no state and needs no volume — everything is configured through
environment variables. This is the deliberate choice for a container image: a deployment running
in Docker is almost always headed somewhere with real Postgres/S3-protocol infrastructure and more
than one person using it, not the single-user `tensorboard`-style setup `serve local` is for.

## When you'd need this

You're running dltrack in Docker/Compose/Kubernetes/ECS rather than installing it directly with
`pip`/`uv tool install`. If you're running it as a plain process on a machine you control, see the
root [README](../README.md#running-it) instead — there's no reason to containerize just to get
`dltrack serve local` or `serve custom` running.

## Running it

It needs a reachable Postgres and an S3-protocol bucket (AWS, MinIO, VAST, ...) to actually start —
both are required, not optional, since the default deployment has no other storage plugin to fall
back to.

To try it with no infrastructure of your own, [`docker-compose.yml`](../docker-compose.yml) at the
repo root is entirely self-contained:

```bash
docker compose up --build
docker compose exec dltrack \
  dltrack users set-password alice --admin --plugins dltrack.scripts.docker_deployment:PLUGINS
```

It builds the image, brings up a throwaway Postgres and an S3-protocol emulation (LocalStack, not
MinIO — see `dltrack/conftest.py`'s `s3_test_server` for why), and creates the bucket dltrack writes
to — nothing to configure first. It is explicitly a local trial, not a deployment: both stores are
backed by Docker volumes that `docker compose down -v` throws away, and `DLTRACK_SECRET_KEY` is a
hardcoded placeholder in the file.

Pointed at your own Postgres/S3 instead, override that same file's environment (or write your own
`docker-compose.yml`/Kubernetes manifest): drop the `localstack`/`s3-init` services, point
`POSTGRES_HOST` and `S3_*` at your real infrastructure, set a real `DLTRACK_SECRET_KEY`
(`openssl rand -hex 32`), and set `S3_ENDPOINT_URL`/`S3_ADDRESSING_STYLE=path` only if that
infrastructure isn't AWS itself.

## Configuration

Every setting is an environment variable — nothing is passed on the container's command line.
These are the ones every deployment needs; the full lists (TLS, pooling, timeouts, presigned
downloads, ...) are in [storage.md](plugins/storage.md) (`PostgresSettings`/`S3Settings`) and
[auth.md](plugins/auth.md) (`PASSWORD_AUTH`'s settings table).

| Variable | Required | |
|---|---|---|
| `POSTGRES_HOST` | yes | Also `_PORT` (5432), `_DATABASE` (`dltrack`), `_USER` (`dltrack`), `_PASSWORD`. |
| `S3_BUCKET` | yes | Also `_ENDPOINT_URL`/`_ADDRESSING_STYLE` for a non-AWS endpoint, `_ACCESS_KEY_ID`/`_SECRET_ACCESS_KEY` (unset falls back to boto3's own credential chain). |
| `DLTRACK_SECRET_KEY` | yes | Signs session cookies for the password auth plugin this image runs. A missing one is a startup error, not a silent fall-through to anonymous access — generate one with `openssl rand -hex 32`. |
| `DLTRACK_ADMIN_USERS` | no | Comma-separated usernames made admin whenever they sign in — the alternative to creating the first admin by hand (below). |

Serve it over HTTPS in front of a reverse proxy — session cookies are `Secure` by default (see
[auth.md](plugins/auth.md) for `DLTRACK_SECURE_COOKIES` and rate-limiting `/login`).

## Creating the first admin

```bash
docker run --rm -it --env-file .env dltrack-server \
  users set-password alice --admin --plugins dltrack.scripts.docker_deployment:PLUGINS
```

`--env-file .env` needs the same `POSTGRES_*`/`S3_*` variables as the running container, since this
writes to the same database. After that, admins can add more users and reset passwords from Admin →
Users in the app itself — see [auth.md](plugins/auth.md).

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

## Running a different plugin list

The image is built with the `postgres` and `s3` extras, but its `ENTRYPOINT`/`CMD` just runs
`dltrack serve custom --plugins dltrack.scripts.docker_deployment:PLUGINS --host 0.0.0.0` — override
either to point somewhere else. To run your own `PLUGINS` module (a different storage backend, an
auth provider you wrote, a different theme), build on top of this image:

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
