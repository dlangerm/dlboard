"""
Browser-driven: the signed-in UI of a deployment with `PASSWORD_AUTH` -- login, a project's Sharing
section, the Account page's API tokens, and the admin Users tab.

Its own app (no chart plugins -- see `dlboard/tests/conftest.py` for why only one app per process
may have them), served on a real socket so the request gate, session cookies, and Dash's
client-side callback-graph validation all run exactly as in production.
"""

from __future__ import annotations

import re
import threading
from typing import TYPE_CHECKING

import pytest
import requests
from playwright.sync_api import expect
from werkzeug.serving import make_server

from dlboard import models
from dlboard._wire import WHOAMI_PATH
from dlboard.conftest import dispose_stores
from dlboard.plugins import BUILTIN_BACKEND, PASSWORD_AUTH
from dlboard.plugins.auth.password import create_or_reset_user
from dlboard.plugins.data_stores import filesystem, sqlite
from dlboard.serve import app as build_app
from dlboard.serve import get_system_data_store
from dlboard.serve._pages._account_page import CREATE_TOKEN_ID, NEW_TOKEN_ID, TOKEN_NAME_ID
from dlboard.serve._pages._project_members import ADD_BUTTON_ID, ADD_GRANTEE_ID

if TYPE_CHECKING:
    from collections.abc import Iterator

    from playwright.sync_api import Page

pytestmark = pytest.mark.browser

_PASSWORD = "correct horse battery"


class _Deployment:
    def __init__(self, url: str, store: models.DataStore[...]) -> None:
        self.url, self.store = url, store


@pytest.fixture(scope="module")
def deployment(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Deployment]:
    tmp_path = tmp_path_factory.mktemp("dlboard-auth-browser")
    monkeypatch = pytest.MonkeyPatch()
    for name, value in {
        "DLBOARD_SQLITE_LOCATION": str(tmp_path / "db.sqlite"),
        "DLBOARD_ARTIFACT_STORE_LOCATION": str(tmp_path / "artifacts"),
        "DLBOARD_SECRET_KEY": "test-secret-at-least-32-characters-long",
        "DLBOARD_SECURE_COOKIES": "false",
    }.items():
        monkeypatch.setenv(name, value)
    try:
        app = build_app([sqlite, filesystem, *PASSWORD_AUTH, *BUILTIN_BACKEND])
    finally:
        monkeypatch.undo()
    app.enable_dev_tools(dev_tools_hot_reload=False, dev_tools_ui=False)
    store = get_system_data_store(app)
    create_or_reset_user(store, "alice", _PASSWORD, admin=True)
    create_or_reset_user(store, "bob", _PASSWORD)
    server = make_server("127.0.0.1", 0, app.server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _Deployment(f"http://127.0.0.1:{server.server_port}", store)
    finally:
        server.shutdown()
        thread.join()
        dispose_stores(app)


def _sign_in(page: Page, deployment: _Deployment, username: str, path: str = "/") -> None:
    page.goto(f"{deployment.url}{path}")
    expect(page).to_have_url(re.compile(r"/login\?next="))
    page.fill("#username", username)
    page.fill("#password", _PASSWORD)
    page.click("button[type=submit]")
    expect(page).to_have_url(f"{deployment.url}{path}")


def _alices_project(store: models.DataStore[...], name: str) -> models.Project:
    alice = store.find_user("alice")
    assert alice is not None
    project = store.create_project(models.NewProject(name=name, description="", created_by=alice.id))
    store.set_project_grant(
        models.NewProjectGrant(project_id=project.id, user_id=alice.id, role=models.ProjectRole.OWNER)
    )
    return project


def test_an_owner_shares_a_project_and_the_member_sees_it_read_only(
    page: Page, deployment: _Deployment, console_errors: list[str]
) -> None:
    project = _alices_project(deployment.store, "shared-with-bob")
    _sign_in(page, deployment, "alice", f"/project/{project.id}")

    page.fill(f"#{ADD_GRANTEE_ID}", "bob")
    page.click(f"#{ADD_BUTTON_ID}")
    expect(page.get_by_role("cell", name="bob", exact=True)).to_be_visible()

    page.context.clear_cookies()
    _sign_in(page, deployment, "bob")
    expect(page.get_by_text("shared-with-bob")).to_be_visible()
    page.goto(f"{deployment.url}/project/{project.id}")
    expect(page.get_by_role("cell", name="alice", exact=True)).to_be_visible()
    expect(page.locator(f"#{ADD_BUTTON_ID}")).to_have_count(0)  # a viewer can't manage members
    assert console_errors == []


def test_a_project_nobody_shared_is_invisible(page: Page, deployment: _Deployment) -> None:
    project = _alices_project(deployment.store, "alice-only")
    _sign_in(page, deployment, "bob")

    expect(page.get_by_text("alice-only")).to_have_count(0)
    page.goto(f"{deployment.url}/project/{project.id}")
    expect(page.get_by_text("doesn't exist, or you don't have access to it")).to_be_visible()


def test_a_token_made_on_the_account_page_authenticates_a_script(
    page: Page, deployment: _Deployment, console_errors: list[str]
) -> None:
    _sign_in(page, deployment, "bob", "/account")

    page.fill(f"#{TOKEN_NAME_ID}", "laptop")
    page.click(f"#{CREATE_TOKEN_ID}")
    notice = page.locator(f"#{NEW_TOKEN_ID}")
    expect(notice).to_contain_text("export DLBOARD_API_KEY=dlb_")
    raw = re.search(r"dlb_\S+", notice.inner_text())
    assert raw is not None

    whoami = requests.get(
        f"{deployment.url}/{WHOAMI_PATH}", headers={"Authorization": f"Bearer {raw.group(0)}"}, timeout=5
    )
    assert whoami.json()["username"] == "bob"

    page.goto(f"{deployment.url}/account")
    expect(page.get_by_role("cell", name="laptop", exact=True)).to_be_visible()
    page.get_by_role("button", name="Revoke").click()
    expect(page.get_by_text("Revoked")).to_be_visible()
    revoked = requests.get(
        f"{deployment.url}/{WHOAMI_PATH}", headers={"Authorization": f"Bearer {raw.group(0)}"}, timeout=5
    )
    assert revoked.status_code == 401
    assert console_errors == []


def test_an_admin_disables_a_user_from_the_users_tab(
    page: Page, deployment: _Deployment, console_errors: list[str]
) -> None:
    create_or_reset_user(deployment.store, "carol", _PASSWORD)
    _sign_in(page, deployment, "alice", "/admin?tab=users")

    carol = page.get_by_role("row").filter(has_text="carol")
    carol.get_by_role("button", name="Disable").click()

    expect(page.get_by_role("row").filter(has_text="carol").get_by_text("Disabled")).to_be_visible()
    disabled = deployment.store.find_user("carol")
    assert disabled is not None
    assert disabled.disabled_at is not None
    assert console_errors == []
