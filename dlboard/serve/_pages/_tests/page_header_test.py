"""The page header every project, experiment and comparison page draws, and what its edit writes back."""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING, Any

import pytest
from dash import html

from dlboard import models
from dlboard.conftest import dispose_stores, find_props
from dlboard.plugins import BUILTIN_BACKEND, LOCAL_AUTH
from dlboard.plugins.data_stores import filesystem, sqlite
from dlboard.serve import _constants as constants
from dlboard.serve import app as build_app
from dlboard.serve._backend._data_store import get_system_data_store
from dlboard.serve._pages._comparison._header import HEADER_IDS as COMPARISON_HEADER_IDS
from dlboard.serve._pages._comparison._ids import COMPARISON_ID_STORE
from dlboard.serve._pages._experiment._header import EXPERIMENT_HEADER_IDS
from dlboard.serve._pages._experiment._page_model import BasicExperimentPage
from dlboard.serve._pages._page_header import NO_DESCRIPTION, PageHeaderIds, page_header
from dlboard.serve._pages._simple_project_page import PROJECT_HEADER_IDS

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from dash import Dash
    from flask.testing import FlaskClient

_IDS = PageHeaderIds[object].of("thing")
_ME = "header-editor"


def _text(component: Any) -> list[str]:  # noqa: ANN401
    """Every plain-string leaf under `component`."""
    if isinstance(component, str):
        return [component]
    if isinstance(component, list):
        return [text for item in component for text in _text(item)]  # pyrefly: ignore [unknown-argument-type]
    if not hasattr(component, "to_plotly_json"):
        return []
    children = component.to_plotly_json()["props"].get("children")  # pyrefly: ignore [unknown-variable-type]
    return _text(children) if children is not None else []  # pyrefly: ignore [unknown-argument-type]


@pytest.mark.parametrize(("description", "shown"), [("does a thing", "does a thing"), ("", NO_DESCRIPTION)])
def test_the_header_shows_its_title_and_description(description: str, shown: str) -> None:
    header = page_header(_IDS, title="my-exp", description=description)

    assert find_props(header, _IDS.title) == {"children": "my-exp", "order": 3, "id": _IDS.title}
    assert (find_props(header, _IDS.description) or {})["children"] == shown


@pytest.mark.parametrize("editable", [True, False])
def test_only_an_editable_header_has_an_edit_icon_and_modal(*, editable: bool) -> None:
    header = page_header(_IDS, title="my-exp", description="does a thing", editable=editable)

    assert (find_props(header, _IDS.edit_button) is not None) is editable
    modal = find_props(header, _IDS.modal)
    assert (modal is not None) is editable
    if modal is not None:
        assert modal["opened"] is False
        assert (find_props(header, _IDS.textarea) or {})["value"] == "does a thing"


def test_the_slots_are_drawn_where_the_page_put_them() -> None:
    header = page_header(
        _IDS,
        title="t",
        description="d",
        actions=[html.Span("ACTION")],
        meta=[html.Span("META")],
        aside=[html.Span("ASIDE")],
    )

    assert _text(header)[:5] == ["t", "ACTION", "d", "META", "ASIDE"]


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Dash]:
    monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("DLBOARD_ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    monkeypatch.setenv("DLBOARD_USER", _ME)  # who the anonymous sign-in says every request is from
    built = build_app([sqlite, filesystem, *LOCAL_AUTH, *BUILTIN_BACKEND])
    yield built
    dispose_stores(built)


def _request(app: Dash, path: str, body: dict[str, Any] | None = None) -> Any:  # noqa: ANN401
    """A request to `app`, in a context of its own (Dash makes the app it serves the process's current one)."""
    client: FlaskClient = app.server.test_client()
    method = "GET" if body is None else "POST"
    return contextvars.copy_context().run(client.open, path, method=method, json=body).json


def _saved(
    app: Dash, ids: PageHeaderIds[Any], entity: tuple[str, int], name: str, description: str
) -> dict[str, Any]:
    """What the browser is sent when `name` and `description` are saved in `ids`' header, as the page itself would."""
    dependencies: list[dict[str, Any]] = _request(app, "/_dash-dependencies")
    (output,) = (str(d["output"]) for d in dependencies if f"{ids.title}.children" in d["output"])
    outputs = [
        dict(zip(("id", "property"), o.rsplit(".", 1), strict=True)) for o in output.strip(".").split("...")
    ]
    entity_id, value = entity
    return _request(
        app,
        "/_dash-update-component",
        {
            "output": output,
            "outputs": outputs,
            "inputs": [{"id": ids.save, "property": "n_clicks", "value": 1}],
            "state": [
                {"id": ids.textarea, "property": "value", "value": description},
                {"id": entity_id, "property": "data", "value": value},
                *([{"id": ids.name, "property": "value", "value": name}] if ids.name is not None else []),
            ],
            "changedPropIds": [f"{ids.save}.n_clicks"],
        },
    )["response"]


@pytest.mark.parametrize("page", ["project", "experiment", "comparison"])
def test_a_saved_description_is_written_in_place_leaving_the_rest_of_the_header_alone(
    app: Dash, page: str
) -> None:
    """
    The save writes the title and the description, not the header: a delete action or a page control survives it.

    Only a comparison's name is edited in its header: a project's and an experiment's keep theirs.
    """
    store = get_system_data_store(app)
    project = store.create_project(models.NewProject(name="proj", description="old"))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id, name="exp"))
    me = store.get_or_create_user(models.Principal.unverified(_ME))
    comparison = store.create_comparison(
        BasicExperimentPage,
        models.NewPage[Any, Any](project_id=project.id, owner_id=me.id, name="cmp"),
        frozenset(),
    )
    ids, entity, title = {
        "project": (PROJECT_HEADER_IDS, (constants.STATE_PROJECT_ID, project.id), "proj"),
        "experiment": (EXPERIMENT_HEADER_IDS, (constants.STATE_EXPERIMENT_ID, experiment.id), "exp"),
        "comparison": (COMPARISON_HEADER_IDS, (COMPARISON_ID_STORE, comparison.id), "renamed"),
    }[page]

    response = _saved(app, ids, entity, " renamed ", "  new words  ")

    assert response == {
        ids.modal: {"opened": False},
        ids.title: {"children": title},
        ids.description: {"children": "new words"},
    }
