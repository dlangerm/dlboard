# pyright: reportPrivateUsage=false
"""
End-to-end: a deployment that signs people in with a password (`dlboard.plugins.PASSWORD_AUTH`).

Drives a real app through Flask's test client -- the request gate, the login form, sessions, API
tokens over REST, and per-user isolation -- rather than any one piece in isolation.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any, NamedTuple

import pendulum
import pytest

from dlboard import models
from dlboard._wire import WHOAMI_PATH, Resource, create_path, get_or_create_path
from dlboard.conftest import dispose_stores
from dlboard.plugins import BUILTIN_BACKEND, PASSWORD_AUTH
from dlboard.plugins.auth.password import SignupPolicy, create_or_reset_user, hash_password, verify_password
from dlboard.plugins.data_stores import filesystem, sqlite
from dlboard.serve import app as build_app
from dlboard.serve import get_system_data_store, mint_api_token

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from flask.testing import FlaskClient

_PASSWORD = "correct horse battery"


class PasswordDeployment(NamedTuple):
    client: FlaskClient
    store: models.DataStore[...]


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[PasswordDeployment]:
    for name, value in {
        "DLBOARD_SQLITE_LOCATION": str(tmp_path / "db.sqlite"),
        "DLBOARD_ARTIFACT_STORE_LOCATION": str(tmp_path / "artifacts"),
        "DLBOARD_SECRET_KEY": "test-secret-at-least-32-characters-long",
        "DLBOARD_SECURE_COOKIES": "false",  # the test client speaks plain HTTP
    }.items():
        monkeypatch.setenv(name, value)
    app = build_app([sqlite, filesystem, *PASSWORD_AUTH, *BUILTIN_BACKEND])
    store = get_system_data_store(app)
    for username in ("alice", "bob"):
        create_or_reset_user(store, username, _PASSWORD)
    yield PasswordDeployment(app.server.test_client(), store)
    dispose_stores(app)


def _csrf(client: FlaskClient, path: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', client.get(path).get_data(as_text=True))
    assert match is not None
    return match.group(1)


def _sign_in(client: FlaskClient, username: str, password: str = _PASSWORD) -> Any:  # noqa: ANN401
    return client.post(
        "/login?next=/admin",
        data={"username": username, "password": password, "csrf": _csrf(client, "/login")},
    )


def _bearer(store: models.DataStore[...], username: str) -> dict[str, str]:
    user = store.find_user(username)
    assert user is not None
    _token, raw = mint_api_token(store, user, "test")
    return {"Authorization": f"Bearer {raw}"}


# -- The request gate -----------------------------------------------------------------------------


def test_a_signed_out_browser_is_sent_to_the_login_form(deployment: PasswordDeployment) -> None:
    browser_accept = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    response = deployment.client.get("/project/1", headers={"Accept": browser_accept})

    assert response.status_code == 302
    assert response.location == "/login?next=/project/1"
    assert deployment.client.get("/login").status_code == 200


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", f"/{get_or_create_path(Resource.PROJECTS)}"),
        ("POST", "/_dash-update-component"),
        ("GET", "/artifact/1"),
    ],
)
def test_anything_else_signed_out_gets_a_401(deployment: PasswordDeployment, method: str, path: str) -> None:
    assert deployment.client.open(path, method=method, json={}).status_code == 401


def test_the_anonymous_username_header_means_nothing_here(deployment: PasswordDeployment) -> None:
    assert deployment.client.get(f"/{WHOAMI_PATH}", headers={"X-Dlboard-User": "alice"}).status_code == 401


def test_every_response_gets_baseline_security_headers(deployment: PasswordDeployment) -> None:
    """Applies even to a 401 -- these are set in `after_request`, which runs regardless of outcome."""
    response = deployment.client.get(f"/{WHOAMI_PATH}")

    assert response.status_code == 401
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Referrer-Policy"] == "same-origin"


def test_oversized_request_bodies_get_a_413(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rejected as the body streams in, not after an unbounded upload already filled memory or disk."""
    for name, value in {
        "DLBOARD_SQLITE_LOCATION": str(tmp_path / "db.sqlite"),
        "DLBOARD_ARTIFACT_STORE_LOCATION": str(tmp_path / "artifacts"),
        "DLBOARD_SECRET_KEY": "test-secret-at-least-32-characters-long",
        "DLBOARD_SECURE_COOKIES": "false",
        "DLBOARD_MAX_UPLOAD_MB": "1",
    }.items():
        monkeypatch.setenv(name, value)
    app = build_app([sqlite, filesystem, *PASSWORD_AUTH, *BUILTIN_BACKEND])
    store = get_system_data_store(app)
    create_or_reset_user(store, "alice", _PASSWORD)
    client = app.server.test_client()
    _sign_in(client, "alice")

    response = client.post(
        f"/{get_or_create_path(Resource.PROJECTS)}", json={"name": "p", "padding": "x" * (2 * 1024 * 1024)}
    )

    assert response.status_code == 413
    dispose_stores(app)


# -- Cross-site request protection -----------------------------------------------------------------
#
# A session cookie is ambient -- any page in the browser can make it send a request, which a bearer
# token (never attached automatically) cannot. `Sec-Fetch-Site`/`Origin` is what tells the two apart.


def test_a_cross_site_post_riding_only_the_session_cookie_is_blocked(
    deployment: PasswordDeployment,
) -> None:
    _sign_in(deployment.client, "alice")

    response = deployment.client.post(
        f"/{get_or_create_path(Resource.PROJECTS)}",
        json={"name": "p"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == 403


def test_a_same_site_post_is_not_blocked(deployment: PasswordDeployment) -> None:
    _sign_in(deployment.client, "alice")

    response = deployment.client.post(
        f"/{get_or_create_path(Resource.PROJECTS)}",
        json={"name": "p"},
        headers={"Sec-Fetch-Site": "same-origin"},
    )

    assert response.status_code == 200


def test_a_cross_origin_post_with_no_sec_fetch_site_header_falls_back_to_origin(
    deployment: PasswordDeployment,
) -> None:
    """A browser old enough to not send `Sec-Fetch-Site` still sends `Origin` on a cross-site request."""
    _sign_in(deployment.client, "alice")

    response = deployment.client.post(
        f"/{get_or_create_path(Resource.PROJECTS)}",
        json={"name": "p"},
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 403


def test_a_cross_site_post_with_a_bearer_token_is_not_blocked(deployment: PasswordDeployment) -> None:
    """A bearer token isn't ambient -- a cross-site page can't attach one, so there's nothing to block."""
    alice = _bearer(deployment.store, "alice")

    response = deployment.client.post(
        f"/{get_or_create_path(Resource.PROJECTS)}",
        json={"name": "p"},
        headers={**alice, "Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == 200


def test_a_cross_site_get_is_never_blocked(deployment: PasswordDeployment) -> None:
    """Safe methods have no side effect to protect, and blocking them would break an ordinary link."""
    _sign_in(deployment.client, "alice")

    response = deployment.client.get(f"/{WHOAMI_PATH}", headers={"Sec-Fetch-Site": "cross-site"})

    assert response.status_code == 200


# -- Signing in -----------------------------------------------------------------------------------


def test_signing_in_starts_a_session_and_returns_to_where_you_were_going(
    deployment: PasswordDeployment,
) -> None:
    response = _sign_in(deployment.client, "alice")

    assert (response.status_code, response.location) == (302, "/admin")
    identity = deployment.client.get(f"/{WHOAMI_PATH}").json
    assert identity is not None
    assert (identity["id"], identity["username"]) == (1, "alice")


@pytest.mark.parametrize(("username", "password"), [("alice", "wrong password"), ("nobody", _PASSWORD)])
def test_a_wrong_username_or_password_is_refused_alike(
    deployment: PasswordDeployment, username: str, password: str
) -> None:
    response = _sign_in(deployment.client, username, password)

    assert response.status_code == 200
    assert "Wrong username or password." in response.get_data(as_text=True)
    assert deployment.client.get(f"/{WHOAMI_PATH}").status_code == 401


def test_repeated_failures_lock_a_username_out(deployment: PasswordDeployment) -> None:
    for _ in range(5):
        _sign_in(deployment.client, "bob", "wrong password")

    assert "Too many attempts" in _sign_in(deployment.client, "bob").get_data(as_text=True)


def test_a_form_post_without_its_csrf_token_is_rejected(deployment: PasswordDeployment) -> None:
    response = deployment.client.post("/login", data={"username": "alice", "password": _PASSWORD})

    assert response.status_code == 400


def test_signing_out_ends_the_session(deployment: PasswordDeployment) -> None:
    _sign_in(deployment.client, "alice")

    deployment.client.get("/sign-out")

    assert deployment.client.get(f"/{WHOAMI_PATH}").status_code == 401


def test_disabling_a_user_ends_their_session(deployment: PasswordDeployment) -> None:
    _sign_in(deployment.client, "alice")
    alice = deployment.store.find_user("alice")
    assert alice is not None

    deployment.store.update_user(alice.model_copy(update={"disabled_at": pendulum.now(pendulum.UTC)}))

    assert deployment.client.get(f"/{WHOAMI_PATH}").status_code == 401


def test_the_server_refuses_to_start_without_a_secret_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("DLBOARD_ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    monkeypatch.delenv("DLBOARD_SECRET_KEY", raising=False)

    with pytest.raises(ValueError, match="DLBOARD_SECRET_KEY"):
        build_app([sqlite, *PASSWORD_AUTH, *BUILTIN_BACKEND])  # no artifact store: no worker to leak


def test_the_server_refuses_to_start_with_the_docker_compose_placeholder_secret_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("DLBOARD_ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    monkeypatch.setenv("DLBOARD_SECRET_KEY", "local-trial-only-change-me")

    with pytest.raises(ValueError, match="placeholder"):
        build_app([sqlite, *PASSWORD_AUTH, *BUILTIN_BACKEND])


def test_the_server_refuses_to_start_with_a_too_short_secret_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("DLBOARD_ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    monkeypatch.setenv("DLBOARD_SECRET_KEY", "too-short")

    with pytest.raises(ValueError, match="characters"):
        build_app([sqlite, *PASSWORD_AUTH, *BUILTIN_BACKEND])


# -- API tokens and per-user isolation ----------------------------------------------------------


def test_a_token_authenticates_a_script_and_a_bad_one_never_falls_back(
    deployment: PasswordDeployment,
) -> None:
    alice = _bearer(deployment.store, "alice")

    identity = deployment.client.get(f"/{WHOAMI_PATH}", headers=alice).json
    assert identity is not None
    assert (identity["id"], identity["username"]) == (1, "alice")
    _sign_in(deployment.client, "bob")
    assert (
        deployment.client.get(
            f"/{WHOAMI_PATH}", headers={"Authorization": "Bearer dlb_bad_token"}
        ).status_code
        == 401
    )


def test_each_user_only_ever_sees_and_writes_their_own_projects(deployment: PasswordDeployment) -> None:
    client, alice, bob = (
        deployment.client,
        _bearer(deployment.store, "alice"),
        _bearer(deployment.store, "bob"),
    )
    projects_path = get_or_create_path(Resource.PROJECTS)
    alices = client.post(f"/{projects_path}", json={"name": "mnist"}, headers=alice).json
    bobs = client.post(f"/{projects_path}", json={"name": "mnist"}, headers=bob).json
    assert alices is not None
    assert bobs is not None
    experiments_path = create_path(Resource.EXPERIMENTS)
    experiment = client.post(f"/{experiments_path}", json={"project_id": alices["id"]}, headers=alice).json
    assert experiment is not None
    run = client.post(
        f"/{create_path(Resource.RUNS)}", json={"experiment_id": experiment["id"]}, headers=alice
    ).json
    assert run is not None

    assert alices["id"] != bobs["id"]
    assert (
        client.post(f"/{experiments_path}", json={"project_id": alices["id"]}, headers=bob).status_code == 403
    )
    metric = {"metrics": {"loss": 1.0}, "step": 0, "experiment_id": experiment["id"], "run_id": run["id"]}
    metric["timestamp_utc"] = pendulum.now(pendulum.UTC).isoformat()
    metrics_path = create_path(Resource.METRICS)
    assert client.post(f"/{metrics_path}", json=[metric], headers=bob).status_code == 403
    assert client.post(f"/{metrics_path}", json=[metric], headers=alice).status_code == 200


def test_an_artifact_you_cannot_see_is_not_found(deployment: PasswordDeployment) -> None:
    store, alice = deployment.store, deployment.store.find_user("alice")
    assert alice is not None
    project = store.create_project(models.NewProject(name="p", description="", created_by=alice.id))
    store.set_project_grant(
        models.NewProjectGrant(project_id=project.id, user_id=alice.id, role=models.ProjectRole.OWNER)
    )
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    run = store.create_run(models.NewRun(experiment_id=experiment.id))
    store.log_artifact_refs(
        [
            models.Artifact(
                key="img", fname="a.png", run_id=run.id, experiment_id=experiment.id, step=0, ref="file:///x"
            )
        ]
    )
    (artifact,) = store.fetch_artifacts(experiment.id)

    assert deployment.client.get(f"/artifact/{artifact.id}", headers=_bearer(store, "bob")).status_code == 404


# -- Signing up and managing passwords --------------------------------------------------------------


def _sign_up(client: FlaskClient, username: str) -> Any:  # noqa: ANN401
    form = {
        "username": username,
        "password": _PASSWORD,
        "confirm": _PASSWORD,
        "csrf": _csrf(client, "/login"),
    }
    return client.post("/signup", data=form)


def test_nobody_can_sign_up_unless_the_deployment_allows_it(deployment: PasswordDeployment) -> None:
    assert deployment.client.get("/signup").status_code == 404


@pytest.mark.parametrize(
    ("policy", "signed_in", "disabled"),
    [(SignupPolicy.OPEN, True, False), (SignupPolicy.APPROVAL, False, True)],
)
def test_signing_up_follows_the_deployments_policy(
    deployment: PasswordDeployment,
    monkeypatch: pytest.MonkeyPatch,
    policy: SignupPolicy,
    signed_in: bool,
    disabled: bool,
) -> None:
    monkeypatch.setenv("DLBOARD_PASSWORD_SIGNUP", policy.value)

    _sign_up(deployment.client, "carol")

    carol = deployment.store.find_user("carol")
    assert carol is not None
    assert (carol.disabled_at is not None) is disabled
    assert (deployment.client.get(f"/{WHOAMI_PATH}").status_code == 200) is signed_in


def test_changing_your_password_signs_out_your_other_sessions(deployment: PasswordDeployment) -> None:
    other_browser = deployment.client.application.test_client()
    _sign_in(other_browser, "alice")
    _sign_in(deployment.client, "alice")
    new = "an even better password"
    form = {
        "current": _PASSWORD,
        "password": new,
        "confirm": new,
        "csrf": _csrf(deployment.client, "/password"),
    }

    assert "Password changed." in deployment.client.post("/password", data=form).get_data(as_text=True)
    assert deployment.client.get(f"/{WHOAMI_PATH}").status_code == 200
    assert other_browser.get(f"/{WHOAMI_PATH}").status_code == 401
    assert _sign_in(other_browser, "alice", new).status_code == 302


def test_only_a_user_manager_can_add_users(deployment: PasswordDeployment) -> None:
    _sign_in(deployment.client, "alice")
    assert deployment.client.get("/password/admin").status_code == 403

    create_or_reset_user(deployment.store, "root", _PASSWORD, admin=True)
    _sign_in(deployment.client, "root")
    form = {"username": "dave", "password": _PASSWORD, "confirm": _PASSWORD}
    form["csrf"] = _csrf(deployment.client, "/password/admin")

    assert "User created for dave." in deployment.client.post("/password/admin", data=form).get_data(
        as_text=True
    )
    assert deployment.store.find_user("dave") is not None


def test_a_password_hash_is_salted_and_verifies_only_its_password() -> None:
    first, second = hash_password(_PASSWORD), hash_password(_PASSWORD)

    assert first != second
    assert verify_password(_PASSWORD, first)
    assert not verify_password("something else", first)


def test_a_script_accepting_anything_gets_a_401_not_the_login_form(deployment: PasswordDeployment) -> None:
    """`requests` sends `Accept: */*` -- redirecting it to a login page would read as a 200."""
    assert deployment.client.get(f"/{WHOAMI_PATH}", headers={"Accept": "*/*"}).status_code == 401


# -- Clock skew between whoever signed the session cookie and whoever reads it --------------------


@pytest.mark.parametrize(
    ("signed_seconds_from_now", "signed_in"),
    [
        (30, True),  # a replica a little ahead: well within the default leeway
        (-1, True),
        (10 * 60, False),  # further ahead than any plausible skew
        (-15 * 24 * 3600, False),  # older than the session lifetime: still expires as ever
    ],
)
def test_a_session_signed_by_a_node_with_a_skewed_clock_is_honoured_within_the_leeway(
    deployment: PasswordDeployment,
    monkeypatch: pytest.MonkeyPatch,
    signed_seconds_from_now: int,
    signed_in: bool,
) -> None:
    """Replicas never agree to the second; a cookie dated slightly ahead used to read as signed out."""
    csrf = _csrf(deployment.client, "/login")
    real_time = time.time
    with monkeypatch.context() as skewed:
        skewed.setattr(time, "time", lambda: real_time() + signed_seconds_from_now)
        deployment.client.post("/login", data={"username": "alice", "password": _PASSWORD, "csrf": csrf})

    assert deployment.client.get(f"/{WHOAMI_PATH}").status_code == (200 if signed_in else 401)
