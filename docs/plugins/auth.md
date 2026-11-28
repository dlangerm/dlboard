# Auth

## What is this

Who is making a request, and what they're allowed to see and do.
It's split in three, and only the first part is a plugin:

| Layer | Owned by | Answers |
| --- | --- | --- |
| **Authentication** | an `AuthProvider` plugin — swappable | How does a *person* prove who they are? |
| **API tokens** | core | How does a *training script* prove who it is? (the same way under every provider) |
| **Authorization** | core, always on | What may this user see or change? |

Built-in providers:

- `anonymous` (`LOCAL_AUTH`, what `dlboard serve local` uses) — nobody signs in.
  A request is attributed to a best-effort name (the client's `X-Dlboard-User` header, `DLBOARD_USER`, or the OS login), which nothing proves, so access control stays off: everyone can see and edit everything, like `tensorboard`.
- `password` (`PASSWORD_AUTH`) — username/password accounts, for hosting dlboard for other people without an identity provider.
- `trusted_header` (`TRUSTED_HEADER_AUTH`) — a reverse proxy that already signed the caller in (oauth2-proxy, Pomerium, Authelia, ...) says who they are in headers.

## When you'd need this

- You're hosting dlboard for more than yourself, and people should only see their own (or their team's) projects: deploy with `PASSWORD_AUTH` (below).
- Your organization already has an identity provider (Okta, Entra ID, Google, Keycloak, ...): put an authenticating proxy in front and deploy with `TRUSTED_HEADER_AUTH` (below), or write a provider for it.
  An OIDC provider is the planned next built-in; the pieces it needs (`Principal.groups`, group grants, `DLBOARD_ADMIN_GROUPS`) are already in place.

## How it works

**Every request goes through one gate.**
`install_request_gate` (`dlboard/serve/_backend/_auth.py`) puts a `before_request` hook in front of every route: pages, Dash callbacks, REST, and `/artifact/<id>`.
In order, it tries:

1. an API token, `Authorization: Bearer dlb_...`.
   A token that doesn't check out is a 401.
   It never falls back to another way of identifying the caller.
2. a signed-in session cookie (only under a provider that verifies identity),
3. the provider's own `authenticate()`.

Whoever that resolves to is bound to the request, and `get_current_user()` reads it back.
Someone it can't resolve is redirected to the provider's `login_url()` if they're a browser asking for a page.
Anything else gets a 401.
Only routes registered with `add_public_route` skip the gate.

**Every store access is authorized.**
`get_data_store()`/`get_artifact_store()` return wrappers bound to the request's user (`dlboard/serve/_backend/_authorization.py`), so no page, callback or REST handler can forget to check, and no storage backend has to know about users.
Background work with no user behind it uses `get_system_data_store()` explicitly.

Access is per project.
Experiments, runs, metrics, artifacts, views and notes inherit it from their project:

| Role | Can |
| --- | --- |
| `viewer` | read everything, post notes, save personal views |
| `editor` | + log runs/metrics/artifacts, create experiments, edit the shared layout, soft-delete within the project, keep a run, hold an experiment to a stricter [retention](../retention.md) rule |
| `owner` | + manage members and sharing, rename or delete the project, set its retention rule, release or delete a kept run |

A role comes from a grant to a user, a grant to an IdP group, or the project's `everyone_role` ("everyone signed in can view/edit").
Whoever creates a project owns it.
Something you can't see reads as missing, exactly like something that doesn't exist.
Site-wide powers are `Scope`s on the user: restore, purge, the audit log, and `user:manage`.
An admin (`Scope.ALL`) is an owner everywhere.

Under `anonymous`, grants aren't enforced: everyone is an editor everywhere, and deleting still takes the scope it always has.
The first user `anonymous` ever sees becomes the admin.
A provider that verifies identity never does that; its admins come from configuration instead.

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

Serve it over HTTPS (session cookies are `Secure` by default), behind a reverse proxy that rate-limits `/login`.
The provider's own throttle is per username, per worker process.

Without a shell, set `DLBOARD_PASSWORD_FIRST_ADMIN_SETUP=true` for the first start instead.
Then open `/setup`, which logs a setup token derived from `DLBOARD_SECRET_KEY` so every worker agrees on it, and enter that token to create the admin.
Turn the setting back off afterwards.
If it's still on once the database has users, the server refuses to start until you turn it off.

The setting is off by default, and even when it's on, `/setup` only works while the database has no users at all.
Once any user exists, `/setup` returns 404 for good.
Demoting every admin doesn't reopen it, and neither does relying on `DLBOARD_ADMIN_USERS`.
Anyone who can read the server log during that first window could create the admin first, so do it before sharing the address.

### Letting people sign up

There is a sign-up page, `/signup`, but it is off by default: on a server anyone can reach, strangers should not be able to make themselves an account unless you say so.
With the default, `DLBOARD_PASSWORD_SIGNUP=admin_creates`, `/signup` answers 404, the login page offers no way to make an account, and an admin creates each one from Admin → Users (or `dlboard users set-password NAME`).
Set it to open the page; the login page then links to it ("No account yet?
Create one"):

- `approval`: anyone can sign up, but the account stays disabled until an admin enables it from Admin → Users.
  Sign-up says so, and the person can sign in once that's done.
- `open`: anyone who can reach the server can sign up and use it straight away.
  Only for a server on a network you trust.

Either way, a new account sees only the projects that let it: one shared with it from the project's Sharing tab, or one that gives everyone a role (which `DLBOARD_NEW_PROJECT_ACCESS`, below, does for each new project).

| Setting | Default | |
| --- | --- | --- |
| `DLBOARD_SECRET_KEY` | — (required) | Signs session cookies. Startup fails without it. |
| `DLBOARD_ADMIN_USERS` | none | Comma-separated usernames made admin whenever they sign in. |
| `DLBOARD_ADMIN_GROUPS` | none | Comma-separated IdP groups whose members are made admin, and stop being one when they leave the group. |
| `DLBOARD_TOKEN_IDLE_DAYS` | unset (never) | Tokens of someone who hasn't signed in for this many days stop working. |
| `DLBOARD_NEW_PROJECT_ACCESS` | unset (private) | `viewer`/`editor`: what everyone signed in gets on a new project. |
| `DLBOARD_SESSION_LIFETIME_HOURS` | 336 | How long a browser stays signed in. |
| `DLBOARD_SECURE_COOKIES` | `true` | Turn off only for plain-HTTP testing. |
| `DLBOARD_PASSWORD_SIGNUP` | `admin_creates` | Who may make an account at `/signup`: `approval` (sign up, then an admin enables you) or `open`. See [Letting people sign up](#letting-people-sign-up). |
| `DLBOARD_PASSWORD_MIN_LENGTH` | 12 | |
| `DLBOARD_PASSWORD_FIRST_ADMIN_SETUP` | `false` | Serve `/setup` for the first admin (above). Startup fails if it's on and the database already has users. |

The rest happens in the app:

- Admins add users and reset passwords from Admin → Users.
  They can also disable users and grant admin there.
- Project owners share a project from its page's Sharing tab.
- Everyone creates API tokens for their training scripts (the home page offers a newcomer their first in one click, and the Account page makes more), then sets `DLBOARD_API_KEY` wherever `DLBoardLogger` runs (see [client.md](../client.md)).

## Behind an authenticating proxy (`TRUSTED_HEADER_AUTH`)

When a reverse proxy in front already signs people in against your identity provider, dlboard can believe the headers it forwards:

```python
# mydeployment.py
from dlboard.plugins import BUILTIN_BACKEND, BUILTIN_CHARTS, POSTGRES_STORAGE, TRUSTED_HEADER_AUTH, themes

PLUGINS = [*POSTGRES_STORAGE, *TRUSTED_HEADER_AUTH, *BUILTIN_BACKEND, *BUILTIN_CHARTS, themes.default]
```

It needs `DLBOARD_TRUSTED_HEADER_ISSUER`, a stable name for the identity source, and `DLBOARD_TRUSTED_HEADER_PROXIES`, the proxy's addresses; the headers it reads are settings too (see [Configuration](../configuration.md#authentication)).
Their defaults are what oauth2-proxy sends with `--pass-user-headers`.
Authelia sends `Remote-User`, `Remote-Email` and `Remote-Groups`; Pomerium sends what its `jwt_claims_headers` names.
`DLBOARD_SECRET_KEY` is needed as for any provider that verifies identity.

- **The headers are believed from the proxy alone.**
  `DLBOARD_TRUSTED_HEADER_PROXIES` is checked against the connection's own address, never against `X-Forwarded-For`, which a client can write.
  A request from anywhere else that carries one of the headers gets a 403, so a network that lets someone reach dlboard around the proxy shows up at once.
  The proxy must also overwrite these headers on every request it forwards, never pass along ones the client sent (oauth2-proxy and Pomerium do).
- **An account is made the first time the proxy names someone.**
  There is nothing to create beforehand, and Admin → Users lists people once they have visited.
  A new account can see only what is shared with everyone (`DLBOARD_NEW_PROJECT_ACCESS`, a project's sharing) or with one of its groups, and can make projects of its own.
  Admins come from `DLBOARD_ADMIN_GROUPS` or `DLBOARD_ADMIN_USERS`; nobody is made admin for signing in first.
- **It is known by its user header, and named by its username header.**
  The user header must carry the identity provider's stable id (an OIDC `sub`): an account is found by it on every request, so a value that changes (an email, a renamed user) makes a new, empty account.
  The username header (`X-Forwarded-Preferred-Username`, which oauth2-proxy sends) is the readable name the account is made with, and what sharing and `DLBOARD_ADMIN_USERS` name it by; without it, the account is named by its user header.
  The name is set once: renaming someone in the identity provider doesn't rename them here, and a new identity under a name another account already has is refused, saying so.
  If people can choose their own username in the identity provider, grant admin by group (`DLBOARD_ADMIN_GROUPS`), not by name: anyone could otherwise take a name in `DLBOARD_ADMIN_USERS` that hasn't signed in yet.
- **A project can be shared with someone once they have visited.**
  Before that, the Sharing tab doesn't know the name; share with one of their groups instead, which works for people who haven't visited yet ([#821](https://github.com/dlangerm/dlboard/issues/821)).
- **When someone leaves, set `DLBOARD_TOKEN_IDLE_DAYS`, or disable them in Admin → Users.**
  Removing them from the identity provider stops the proxy signing them in, but a token never goes through the proxy.
  With `DLBOARD_TOKEN_IDLE_DAYS` set, their tokens (API and run tokens) stop working once they haven't signed in through the proxy for that many days; unset, the tokens keep working until each expires or is revoked.
  Disabling them stops everything at once: a disabled user can't use the app or any token.
  Their groups stay as the proxy last sent them until they sign in again ([#822](https://github.com/dlangerm/dlboard/issues/822)).
- **Admin from a group lasts as long as the group does.**
  Someone made admin by `DLBOARD_ADMIN_GROUPS` alone stops being one the first time the proxy signs them in without the group.
  An admin made any other way (by `DLBOARD_ADMIN_USERS`, or from Admin → Users) is kept until it's taken away there.
- **A training script signs in with an API token, not the proxy.**
  A token works from anywhere, so a script can talk to dlboard directly or through the proxy, if the proxy lets bearer tokens through.
- **Signing out is the proxy's to do:** the headers sign a browser in again on its next request.
- An AWS ALB's or Cloudflare Access's signed assertion (`x-amzn-oidc-data`, `Cf-Access-Jwt-Assertion`) is not checked: put oauth2-proxy (or another proxy that forwards plain headers) behind them, or wait for the OIDC provider.

## How do I build a provider

Implement `AuthProvider` (`dlboard/models/_auth.py`) and register it from `plug(app)`.
For example, behind a proxy that has already authenticated the caller:

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

- **A `Principal` is keyed by `(issuer, subject)`, not by username.**
  `subject` must be the identity provider's stable id (an OIDC `sub`).
  A user who is renamed keeps their projects, and two identity providers presenting the same username can't take over each other's accounts.
- **`authenticate()` is called on every request that has no API token and no session.**
  Return `None` if your provider signs people in through a form or redirect instead.
  In that case, register those routes with `add_public_route`.
  Once the person proves who they are, call `sign_in(store, principal, get_auth_settings())`, then `start_session(user)`.
  See `password.py`.
- **Check configuration that needs the data store with `add_startup_check(app, check)`.**
  Plugin order isn't guaranteed, so the store may not be set yet during your `plug()`.
  A registered check runs once every plugin has plugged in, and startup fails if it raises.
- **Only set `verifies_identity = True` if the provider really proves identity.**
  It turns on access control, and that's only as strong as the provider's proof.
  `trusted_header`-style providers must only be reachable through the proxy.
- **Only one provider per app.**
  `set_auth_provider` refuses a second one.
