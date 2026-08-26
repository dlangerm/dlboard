# Auth plugins

## What is this

An auth plugin implements `AuthProvider` (`dltrack/serve/_backend/_auth.py`) — one method,
`resolve_identity(self) -> str | None`, that answers "who is making this request" for both REST
calls and in-process Dash callbacks. `get_current_user(store)` is the single place identity gets
turned into a `User` row (get-or-create by username); everything else in the app calls that
instead of resolving identity itself.

## When you'd need this

The built-in `AnonymousAuthProvider` (`dltrack/plugins/auth/anonymous.py`) never rejects a
request — it's attribution, not authentication, meant for a local single-user deployment. You'd
write a real provider to add actual login/session flows, or to integrate with an external identity
system (SSO/OIDC, an enterprise directory) so a deployment can manage its users outside of dltrack.

## How do I build one

Implement `AuthProvider[...]`:

```python
class MyAuthProvider:
    def resolve_identity(self) -> str | None: ...

    @classmethod
    def get_or_create(cls) -> MyAuthProvider: ...
```

then register it from `plug(app)`:

```python
def plug(app: Dash) -> None:
    set_auth_provider(app, MyAuthProvider.get_or_create())
```

Only one auth provider can be set per app — `set_auth_provider` raises if one's already registered,
so a deployment picks exactly one (swap `anonymous` out for yours in the plugin list, don't add
both). `resolve_identity` returning `None` falls back to `ANONYMOUS` in `get_current_user` — decide
deliberately whether your provider should ever do that, or should reject the request instead (e.g.
by raising before `resolve_identity` is even reached, at the Flask request level, for a provider
that actually needs to gate access rather than just attribute it).

`anonymous.py` is the reference to read for the REST-vs-callback distinction: REST calls carry an
`X-Dltrack-User` header the client sets; in-process Dash callbacks have no request to read a header
from, so they fall back to the server's own environment/OS-login resolution
(`dltrack._identity.resolve_username`).
