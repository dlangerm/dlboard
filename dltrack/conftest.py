# pyright: reportPrivateUsage=false
"""Shared fixtures and Dash-component helpers for the test suite."""

from __future__ import annotations

import threading
import typing
import uuid
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

import pytest
import sqlalchemy as sa
from flask import Flask
from pydantic import AnyHttpUrl, SecretStr
from werkzeug.serving import make_server

from dltrack import models
from dltrack.plugins import BUILTIN_BACKEND, LOCAL_AUTH
from dltrack.plugins.backend import artifact_purge_worker
from dltrack.plugins.data_stores import filesystem, postgres, s3, sqlite
from dltrack.plugins.data_stores._blob_store import BlobArtifactStore
from dltrack.plugins.data_stores.postgres import PostgresSettings, PostgresStore
from dltrack.plugins.data_stores.s3 import S3DownloadMode, S3Settings
from dltrack.plugins.data_stores.sqlite import SQLLiteStore
from dltrack.serve import SQLStoreBase, get_system_data_store
from dltrack.serve import app as build_app
from dltrack.serve._backend import _auth
from dltrack.serve._backend._data_store import ARTIFACT_STORE

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from dash import Dash

    from dltrack.models import PluginProtocol
    from dltrack.plugins.data_stores._blob_store import BlobBackend


class ScreenshotMode(StrEnum):
    """What the `screenshots`-marked tests do with the docs images (`--screenshots`)."""

    CHECK = "check"
    """Fail if a freshly rendered screenshot differs from the committed one."""

    UPDATE = "update"
    """Overwrite the committed screenshots with freshly rendered ones."""


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register `--screenshots`, the switch for the docs-screenshot tests (`docs_screenshots_test.py`)."""
    parser.addoption(
        "--screenshots",
        type=ScreenshotMode,
        choices=list(ScreenshotMode),
        default=None,
        help="Run the docs-screenshot tests: `check` against, or `update`, the images in docs/images.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """
    Keep the docs-screenshot tests out of ordinary runs, and out of everything else in their own.

    Their images depend on the whole database (the home page lists every project), so a run that
    asks for screenshots renders them in a process that ran nothing else.
    """
    screenshots_requested = config.getoption("--screenshots") is not None
    if screenshots_requested:
        deselected = [item for item in items if item.get_closest_marker("screenshots") is None]
        config.hook.pytest_deselected(items=deselected)
        items[:] = [item for item in items if item not in deselected]
        return
    skip = pytest.mark.skip(reason="docs screenshots are rendered by CI; pass --screenshots=check|update")
    for item in items:
        if item.get_closest_marker("screenshots") is not None:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def screenshot_mode(request: pytest.FixtureRequest) -> ScreenshotMode:
    """The `--screenshots` mode this run was started with."""
    return cast("ScreenshotMode", request.config.getoption("--screenshots"))


class StoreBackend(StrEnum):
    """Which database the `store`/`backend_server` fixtures are backed by (see `store_backend`)."""

    SQLITE = "sqlite"
    POSTGRES = "postgres"


EVERY_STORE_BACKEND: list[Any] = [
    StoreBackend.SQLITE,
    pytest.param(StoreBackend.POSTGRES, marks=pytest.mark.postgres),
]
"""
`params` for a module's own `store_backend` override, to run its tests against every backend:

    @pytest.fixture(params=EVERY_STORE_BACKEND)
    def store_backend(request: pytest.FixtureRequest) -> StoreBackend:
        return request.param
"""


@pytest.fixture(scope="session")
def postgres_server() -> Iterator[PostgresSettings]:
    """A real Postgres in a throwaway container (needs Docker), started once per test session."""
    from testcontainers.community.postgres import (
        PostgresContainer,
    )

    with PostgresContainer(
        "postgres:17-alpine", username="dltrack", password="dltrack", dbname="dltrack"
    ) as pg:
        yield PostgresSettings(
            host=pg.get_container_host_ip(),
            port=pg.get_exposed_port(5432),
            database="dltrack",
            user="dltrack",
            password=SecretStr("dltrack"),
            sslmode="disable",
        )


@pytest.fixture
def postgres_settings(postgres_server: PostgresSettings) -> Iterator[PostgresSettings]:
    """Settings for a fresh, empty schema of its own in the session's Postgres, dropped afterwards."""
    settings = postgres_server.model_copy(update={"db_schema": f"test_{uuid.uuid4().hex[:12]}"})
    yield settings
    admin = sa.create_engine("postgresql+psycopg://", connect_args=postgres_server.libpq_connect_args())
    with admin.begin() as conn:
        conn.execute(sa.schema.DropSchema(settings.db_schema, cascade=True, if_exists=True))
    admin.dispose()


@pytest.fixture
def store_backend() -> StoreBackend:
    """sqlite, unless a module overrides this to run against every backend (see `EVERY_STORE_BACKEND`)."""
    return StoreBackend.SQLITE


class ArtifactBackend(StrEnum):
    """Which `BlobBackend` the `artifact_backend`-parametrized fixtures are backed by."""

    FILESYSTEM = "filesystem"
    S3 = "s3"


EVERY_ARTIFACT_BACKEND: list[Any] = [
    ArtifactBackend.FILESYSTEM,
    pytest.param(ArtifactBackend.S3, marks=pytest.mark.s3),
]
"""
`params` for a module's own `artifact_backend` override, to run its tests against every backend:

    @pytest.fixture(params=EVERY_ARTIFACT_BACKEND)
    def artifact_backend(request: pytest.FixtureRequest) -> ArtifactBackend:
        return request.param
"""


@pytest.fixture(scope="session")
def s3_test_server() -> Iterator[S3Settings]:
    """
    A real S3-protocol endpoint in a throwaway container (needs Docker), started once per session.

    LocalStack's S3 emulation, not MinIO: MinIO's own container images now require a Docker
    Hub/quay.io login, which a public CI runner doesn't have. Either way, this exercises the exact
    same `S3Blobs` code path a real MinIO/VAST deployment would -- a non-AWS `endpoint_url`, with
    path-style addressing (`addressing_style="path"`), both settings that matter equally for any
    S3-protocol store that isn't AWS itself.
    """
    from testcontainers.community.localstack import LocalStackContainer

    bucket = "dltrack-test"
    with LocalStackContainer() as localstack:
        localstack.get_client("s3").create_bucket(  # pyright: ignore[reportUnknownMemberType, reportCallIssue]
            Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": localstack.region_name}
        )
        yield S3Settings(
            bucket=bucket,
            endpoint_url=AnyHttpUrl(localstack.get_url()),
            access_key_id="testcontainers-localstack",
            secret_access_key=SecretStr("testcontainers-localstack"),
            region=localstack.region_name,
            addressing_style="path",
            verify_tls=False,
        )


@pytest.fixture
def s3_settings(s3_test_server: S3Settings) -> S3Settings:
    """Settings for a fresh prefix of its own in the session's S3 test bucket."""
    return s3_test_server.model_copy(update={"prefix": f"test-{uuid.uuid4().hex[:12]}"})


@pytest.fixture
def artifact_backend() -> ArtifactBackend:
    """Filesystem, unless a module overrides this to run against every backend (see `EVERY_ARTIFACT_BACKEND`)."""
    return ArtifactBackend.FILESYSTEM


@pytest.fixture
def s3_download_mode(request: pytest.FixtureRequest) -> S3DownloadMode:
    """
    `backend_server`'s S3 branch proxies by default.

    A test parametrizes `s3_download_mode` indirectly (`@pytest.mark.parametrize("s3_download_mode",
    [S3DownloadMode.PRESIGN], indirect=True)`) to exercise presigning instead.
    """
    return cast("S3DownloadMode", getattr(request, "param", S3DownloadMode.PROXY))


@pytest.fixture
def blob_backend(
    artifact_backend: ArtifactBackend, tmp_path: Path, request: pytest.FixtureRequest
) -> BlobBackend:
    """A throwaway `BlobBackend` for a single test, backed by `artifact_backend`."""
    match artifact_backend:
        case ArtifactBackend.FILESYSTEM:
            return filesystem.FSBlobs(tmp_path)
        case ArtifactBackend.S3:
            return s3.S3Blobs(request.getfixturevalue("s3_settings"))


@pytest.fixture
def store(
    store_backend: StoreBackend, tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[SQLStoreBase[Any]]:
    """A throwaway store for a single test, backed by `store_backend`."""
    match store_backend:
        case StoreBackend.SQLITE:
            yield SQLLiteStore(tmp_path / "test.sqlite")
        case StoreBackend.POSTGRES:
            postgres = PostgresStore(request.getfixturevalue("postgres_settings"))
            yield postgres
            postgres.dispose()


@pytest.fixture
def experiment_id(store: SQLStoreBase[Any]) -> int:
    """A freshly created project/experiment pair, returning the experiment's id."""
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    return experiment.id


@pytest.fixture
def admin(store: SQLStoreBase[Any]) -> models.User:
    """The bootstrap admin -- the first user any fresh store creates gets `Scope.ALL`."""
    return store.get_or_create_user(models.Principal.unverified("admin"))


@pytest.fixture
def sign_in_as(store: SQLStoreBase[Any]) -> Iterator[Callable[[str], models.User]]:
    """
    Make `get_current_user()` resolve to `username` for the rest of this test, outside a real Dash app.

    Pushes a bare Flask request context for the whole test and binds the user to it, exactly as the
    request gate does for a real request. Call the returned setter again with a different name to
    switch identities mid-test (e.g. to simulate two different people editing the same page).
    """
    with Flask(__name__).test_request_context():

        def _sign_in(username: str) -> models.User:
            user = store.get_or_create_user(models.Principal.unverified(username))
            _auth.bind_current_user(user)
            return user

        yield _sign_in


class EntityChain(typing.NamedTuple):
    """IDs for a project -> experiment -> run(-> artifact) chain, for tests that need real rows."""

    project_id: int
    experiment_id: int
    run_id: int
    artifact_id: int | None


def create_entity_chain(store: SQLStoreBase[Any], *, artifact: bool = False) -> EntityChain:
    """
    Create a project/experiment/run, and optionally one artifact.

    For tests exercising cascade/soft-delete/audit-log/purge behavior that need a real row at
    each level rather than caring about any of their particular names or content.
    """
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    run = store.create_run(models.NewRun(experiment_id=experiment.id))
    artifact_id = None
    if artifact:
        store.log_artifact_refs(
            [
                models.Artifact(
                    key="img",
                    fname="i.png",
                    run_id=run.id,
                    experiment_id=experiment.id,
                    step=0,
                    ref="ref://a",
                )
            ]
        )
        (logged,) = list(store.fetch_artifacts(experiment_id=experiment.id))
        assert logged.id is not None
        artifact_id = logged.id
    return EntityChain(project.id, experiment.id, run.id, artifact_id)


def dispose_stores(app: Dash) -> None:
    """
    Release `app`'s storage: its database pool, and its artifact store's worker process if it has one.

    Every test that builds its own app needs this -- a filesystem/S3 artifact store spawns a worker
    process per app, and enough leaked ones slow the rest of the session to a crawl.
    """
    data_store = get_system_data_store(app)
    if isinstance(data_store, SQLStoreBase):
        data_store.dispose()
    artifact_store = ARTIFACT_STORE.find(app)
    if isinstance(artifact_store, BlobArtifactStore):
        artifact_store.dispose()


class BackendServer(typing.NamedTuple):
    """A live dltrack backend's URL, plus a store reading the same database it writes to."""

    url: str
    store: SQLStoreBase[Any]


@pytest.fixture
def backend_server(
    store_backend: StoreBackend,
    artifact_backend: ArtifactBackend,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> Iterator[BackendServer]:
    """
    A real dltrack backend (storage + REST routes, no charts) on a background thread.

    Metadata storage is on `store_backend`, artifact (blob) storage is on `artifact_backend` --
    independent axes, since `DataStore` and `ArtifactStore` are entirely separate plugins.

    Deliberately leaves out `BUILTIN_CHARTS`: chart plugins register into a process-global registry
    that rejects a second registration, so only one app per pytest process may include them
    (`browser_test.py`'s). Everything this fixture builds can be built any number of times.
    """
    monkeypatch = pytest.MonkeyPatch()
    store: SQLStoreBase[Any]
    metadata_plugin: PluginProtocol
    match store_backend:
        case StoreBackend.SQLITE:
            monkeypatch.setenv("SQLITE_LOCATION", str(tmp_path / "test.sqlite"))
            metadata_plugin, store = sqlite, SQLLiteStore(tmp_path / "test.sqlite")
        case StoreBackend.POSTGRES:
            settings: PostgresSettings = request.getfixturevalue("postgres_settings")
            for name, value in settings.model_dump(
                include={"host", "port", "database", "user", "db_schema"}
            ).items():
                monkeypatch.setenv(f"POSTGRES_{name.upper()}", str(value))
            monkeypatch.setenv(
                "POSTGRES_PASSWORD", settings.password.get_secret_value() if settings.password else ""
            )
            monkeypatch.setenv("POSTGRES_SSLMODE", settings.sslmode)
            metadata_plugin, store = postgres, PostgresStore(settings)
    artifact_plugin: PluginProtocol
    match artifact_backend:
        case ArtifactBackend.FILESYSTEM:
            monkeypatch.setenv("ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
            artifact_plugin = filesystem
        case ArtifactBackend.S3:
            s3_config: S3Settings = request.getfixturevalue("s3_settings")
            for name, value in s3_config.model_dump(
                include={
                    "bucket",
                    "prefix",
                    "endpoint_url",
                    "access_key_id",
                    "addressing_style",
                    "verify_tls",
                }
            ).items():
                monkeypatch.setenv(f"S3_{name.upper()}", str(value))
            monkeypatch.setenv(
                "S3_SECRET_ACCESS_KEY",
                s3_config.secret_access_key.get_secret_value() if s3_config.secret_access_key else "",
            )
            monkeypatch.setenv("S3_DOWNLOAD_MODE", request.getfixturevalue("s3_download_mode").value)
            artifact_plugin = s3
    try:
        app = build_app(
            [metadata_plugin, artifact_plugin, artifact_purge_worker, *LOCAL_AUTH, *BUILTIN_BACKEND]
        )
    finally:
        monkeypatch.undo()
    server = make_server("127.0.0.1", 0, app.server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield BackendServer(f"http://127.0.0.1:{server.server_port}", store)
    finally:
        server.shutdown()
        thread.join()
        store.dispose()
        dispose_stores(app)


def props(component: object) -> dict[str, Any]:
    """
    Extract a rendered Dash component's props.

    dash-mantine-components ships no py.typed marker, so its component attrs are Unknown to
    pyright regardless of how this is typed.
    """
    return cast("Any", component).to_plotly_json()["props"]


def find_props(component: Any, target_id: object) -> dict[str, Any] | None:  # noqa: ANN401
    """Depth-first search a dash component tree for the props of a node with `target_id`."""
    if isinstance(component, list):
        for item in cast("list[Any]", component):
            found = find_props(item, target_id)
            if found is not None:
                return found
        return None
    if not hasattr(component, "to_plotly_json"):
        return None
    component_props = props(component)
    if component_props.get("id") == target_id:
        return component_props
    children = component_props.get("children")
    return find_props(children, target_id) if children is not None else None
