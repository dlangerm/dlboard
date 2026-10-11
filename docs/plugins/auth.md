# Auth

## What is this

Who is making a request, and what they're allowed to see and do. It's split in three, and only the
first part is a plugin:

| Layer | Owned by | Answers |
|---|---|---|
| **Authentication** | an `AuthProvider` plugin — swappable | How does a *person* prove who they are? |
| **API tokens** | core | How does a *training script* prove who it is? (the same way under every provider) |
| **Authorization** | core, always on | What may this user see or change? |

Built-in providers:

- `anonymous` (`LOCAL_AUTH`, what `dlboard serve local` uses) — nobody signs in. A request is
  attributed to a best-effort name (the client's `X-Dlboard-User` header, `DLBOARD_USER`, or the OS
  login), which nothing proves, so access control stays off: everyone can see and edit everything,
  like `tensorboard`.
- `password` (`PASSWORD_AUTH`) — username/password accounts, for hosting dlboard for other people
  without an identity provider.

## When you'd need this

- You're hosting dlboard for more than yourself, and people should only see their own (or their
  team's) projects: deploy with `PASSWORD_AUTH` (below).
- Your organization already has an identity provider (Okta, Entra ID, Google, Keycloak, ...):
  write a provider for it (below). An OIDC provider and a "trusted reverse-proxy header" provider
  (for oauth2-proxy, Pomerium, an AWS ALB, Cloudflare Access) are the planned next built-ins; the
  pieces they need (`Principal.groups`, group grants, `DLBOARD_ADMIN_GROUPS`) are already in place.

## How it works

**Every request goes through one gate.** `install_request_gate` (`dlboard/serve/_backend/_auth.py`)
puts a `before_request` hook in front of every route: pages, Dash callbacks, REST, and
`/artifact/<id>`. In order, it tries:

1. an API token, `Authorization: Bearer dlb_...`. A token that doesn't check out is a 401. It
   never falls back to another way of identifying the caller.
2. a signed-in session cookie (only under a provider that verifies identity),
3. the provider's own `authenticate()`.

Whoever that resolves to is bound to the request, and `get_current_user()` reads it back. Someone
it can't resolve is redirected to the provider's `login_url()` if they're a browser asking for a
page. Anything else gets a 401. Only routes registered with `add_public_route` skip the gate.

**Every store access is authorized.** `get_data_store()`/`get_artifact_store()` return wrappers
bound to the request's user (`dlboard/serve/_backend/_authorization.py`), so no page, callback or
REST handler can forget to check, and no storage backend has to know about users. Background work
with no user behind it uses `get_system_data_store()` explicitly.

Access is per project. Experiments, runs, metrics, artifacts, views and notes inherit it from
their project:

| Role | Can |
|---|---|
| `viewer` | read everything, post notes, save personal views |
| `editor` | + log runs/metrics/artifacts, create experiments, edit the shared layout, soft-delete within the project |
| `owner` | + manage members and sharing, rename or delete the project |

A role comes from a grant to a user, a grant to an IdP group, or the project's `everyone_role`
("everyone signed in can view/edit"). Whoever creates a project owns it. Something you can't see
reads as missing, exactly like something that doesn't exist. Site-wide powers are `Scope`s on the
user: restore, purge, the audit log, and `user:manage`. An admin (`Scope.ALL`) is an owner
everywhere.

Under `anonymous`, grants aren't enforced: everyone is an editor everywhere, and deleting still
takes the scope it always has. The first user `anonymous` ever sees becomes the admin. A provider
that verifies identity never does that; its admins come from configuration instead.

## Hosting dlboard for other people (`PASSWORD_AUTH`)

```python
# mydeployment.py
from dlboard.plugins import BUILTIN_BACKEND, BUILTIN_CHARTS, PASSWORD_AUTH, POSTGRES_S3_STORAGE, themes

PLUGINS = [*POSTGRES_S3_STORAGE, *PASSWORD_AUTH, *BUILTIN_BACKEND, *BUILTIN_CHARTS, themes.default]
```

```bash
export DLBOARD_SECRET_KEY=$(openssl rand -hex 32)   # signs sessions; the same for every worker
dlboard serve custom --plugins mydeployment:PLUGINS --host 0.0.0.0 --workers 4
dlboard users set-password alice --admin --plugins mydeployment:PLUGINS   # your first admin
```

Serve it over HTTPS (session cookies are `Secure` by default), behind a reverse proxy that
rate-limits `/login`. The provider's own throttle is per username, per worker process.

Without a shell, set `DLBOARD_PASSWORD_FIRST_ADMIN_SETUP=true` for the first start instead. Then open
`/setup`, which logs a setup token derived from `DLBOARD_SECRET_KEY` so every worker agrees on it, and
enter that token to create the admin. Turn the setting back off afterwards. If it's still on once the
database has users, the server refuses to start until you turn it off.

The setting is off by default, and even when it's on, `/setup` only works while the database has no users
at all. Once any user exists, `/setup` returns 404 for good. Demoting every admin doesn't reopen it, and
neither does relying on `DLBOARD_ADMIN_USERS`. Anyone who can read the server log during that first window
could create the admin first, so do it before sharing the address.

| Setting | Default | |
|---|---|---|
| `DLBOARD_SECRET_KEY` | — (required) | Signs session cookies. Startup fails without it. |
| `DLBOARD_ADMIN_USERS` | none | Comma-separated usernames made admin whenever they sign in. |
| `DLBOARD_ADMIN_GROUPS` | none | Comma-separated IdP groups whose members are made admin. |
| `DLBOARD_NEW_PROJECT_ACCESS` | unset (private) | `viewer`/`editor`: what everyone signed in gets on a new project. |
| `DLBOARD_SESSION_LIFETIME_HOURS` | 336 | How long a browser stays signed in. |
| `DLBOARD_SECURE_COOKIES` | `true` | Turn off only for plain-HTTP testing. |
| `DLBOARD_PASSWORD_SIGNUP` | `admin_creates` | `approval` (sign up, then an admin enables you) or `open`. |
| `DLBOARD_PASSWORD_MIN_LENGTH` | 12 | |
| `DLBOARD_PASSWORD_FIRST_ADMIN_SETUP` | `false` | Serve `/setup` for the first admin (above). Startup fails if it's on and the database already has users. |

The rest happens in the app:

- Admins add users and reset passwords from Admin → Users. They can also disable users and grant
  admin there.
- Project owners share a project from its page's Sharing section.
- Everyone creates API tokens for their training scripts on their Account page, then sets
  `DLBOARD_API_KEY` wherever `DLBoardLogger` runs (see [client.md](../client.md)).

## How do I build a provider

Implement `AuthProvider` (`dlboard/models/_auth.py`) and register it from `plug(app)`. For example,
behind a proxy that has already authenticated the caller:

```python
from typing import ClassVar

from flask import request

from dlboard.models import Principal
from dlboard.serve import set_auth_provider


class ProxyHeaderProvider:
    display_name: ClassVar[str] = "Company SSO"
    verifies_identity: ClassVar[bool] = True
    manage_url: ClassVar[str | None] = None  # where a user manages their own sign-in
    admin_url: ClassVar[str | None] = None  # where an admin adds users; None = users come from the IdP

    def authenticate(self) -> Principal | None:
        email = request.headers.get("X-Forwarded-Email")
        if email is None:
            return None
        groups = frozenset(filter(None, request.headers.get("X-Forwarded-Groups", "").split(",")))
        return Principal(
            issuer="https://sso.example.com",
            subject=email,
            username=email.split("@")[0],
            email=email,
            groups=groups,
        )

    def login_url(self, next_path: str) -> str | None:
        return None  # the proxy handles sign-in before a request ever gets here

    @classmethod
    def get_or_create(cls) -> "ProxyHeaderProvider":
        return cls()


def plug(app) -> None:
    set_auth_provider(app, ProxyHeaderProvider.get_or_create())
```

- **A `Principal` is keyed by `(issuer, subject)`, not by username.** `subject` must be the
  identity provider's stable id (an OIDC `sub`). A user who is renamed keeps their projects, and
  two identity providers presenting the same username can't take over each other's accounts.
- **`authenticate()` is called on every request that has no API token and no session.** Return
  `None` if your provider signs people in through a form or redirect instead. In that case,
  register those routes with `add_public_route`. Once the person proves who they are, call
  `sign_in(store, principal, get_auth_settings())`, then `start_session(user)`. See `password.py`.
- **Check configuration that needs the data store with `add_startup_check(app, check)`.** Plugin
  order isn't guaranteed, so the store may not be set yet during your `plug()`. A registered check
  runs once every plugin has plugged in, and startup fails if it raises.
- **Only set `verifies_identity = True` if the provider really proves identity.** It turns on
  access control, and that's only as strong as the provider's proof. `trusted_header`-style
  providers must only be reachable through the proxy.
- **Only one provider per app.** `set_auth_provider` refuses a second one.
