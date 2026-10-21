"""Shared fixtures and Dash-component helpers for the test suite."""

from __future__ import annotations

import threading
import typing
import uuid
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pandas as pd
import pytest
import sqlalchemy as sa
from flask import Flask
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client.parser import text_string_to_metric_families
from pydantic import AnyHttpUrl, BaseModel, SecretStr, TypeAdapter
from werkzeug.serving import make_server

from dlboard import models
from dlboard._telemetry import _sdk
from dlboard._telemetry.traces import TRACES
from dlboard.plugins import BUILTIN_BACKEND, LOCAL_AUTH
from dlboard.plugins.backend import artifact_purge_worker
from dlboard.plugins.data_stores import filesystem, postgres, s3, sqlite
from dlboard.plugins.data_stores._blob_store import BlobArtifactStore
from dlboard.plugins.data_stores.postgres import PostgresSettings, PostgresStore
from dlboard.plugins.data_stores.s3 import S3DownloadMode, S3Settings
from dlboard.plugins.data_stores.sqlite import SQLLiteStore
from dlboard.serve import SQLStoreBase, get_system_data_store
from dlboard.serve import app as build_app
from dlboard.serve._backend import _auth
from dlboard.serve._backend._data_store import ARTIFACT_STORE
from dlboard.serve.telemetry import LOGS, METRICS, _instrument

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from wsgiref.types import InputStream, StartResponse, WSGIEnvironment

    from dash import Dash
    from opentelemetry.proto.trace.v1.trace_pb2 import Span as WireSpan

    from dlboard.models import PluginProtocol
    from dlboard.plugins.data_stores._blob_store import BlobBackend


class ScreenshotMode(StrEnum):
    """What the `screenshots`-marked tests do with the docs images (`--screenshots`)."""

    CHECK = "check"
    """Fail if a freshly rendered screenshot differs from the committed one."""

    UPDATE = "update"
    """Overwrite the committed screenshots with freshly rendered ones."""


class DurationTier(IntEnum):
    """
    How long a test takes, from its recorded duration in `test-durations.json`.

    Ordered so sorting runs the cheap tests first. Each member's name is also the marker it applies,
    so `-m huge` or `-m "not long"` selects by tier.
    """

    SHORT = 0
    MEDIUM = 1
    LONG = 2
    HUGE = 3

    @property
    def marker(self) -> pytest.MarkDecorator:
        """The pytest marker for this tier, registered in `pyproject.toml`."""
        return getattr(pytest.mark, self.name.lower())


DURATIONS_PATH = Path(__file__).parents[1] / "test-durations.json"
"""Per-test wall time in seconds, from the last `--record-durations` run; committed so CI sorts the same way."""

MAX_HUGE_TESTS = 5
"""The most tests the suite may have in the `huge` tier; collection fails past it."""

_TIER_THRESHOLDS: tuple[tuple[float, DurationTier], ...] = (
    (60.0, DurationTier.HUGE),
    (30.0, DurationTier.LONG),
    (10.0, DurationTier.MEDIUM),
)
"""A test lands in the first tier whose threshold its duration exceeds, so 61s is huge and 30s is medium."""

_MEASURED = pytest.StashKey[dict[str, float]]()
"""This run's per-test wall time, summed over setup, call and teardown -- what `--record-durations` saves."""

COLLECTED_TESTS = pytest.StashKey[frozenset[str] | None]()
"""
Every test this run collected, before `-k`/`-m` deselect any -- or `None` if it collected only part of the suite.

Only a run that collected everything can tell a deleted test from one it just didn't look at, so only that run
prunes `test-durations.json` and checks it against the suite (`durations_test.py`).
"""

_DURATIONS = TypeAdapter(dict[str, float])


def _collected_whole_suite(session: pytest.Session) -> bool:
    """
    Whether this run collected every test: no paths of its own, nothing ignored, no `--lf`, no collection errors.

    `-k` and `-m` only deselect after collection, so a run filtered by them still collected everything.
    """
    config = session.config
    return (
        config.args_source is pytest.Config.ArgsSource.TESTPATHS
        and not config.getoption("ignore")
        and not config.getoption("ignore_glob")
        and not config.getoption("lf", default=False)
        and session.testsfailed == 0
    )


def load_durations() -> dict[str, float]:
    """The recorded durations, or nothing if no run has recorded any yet."""
    if not DURATIONS_PATH.exists():
        return {}
    return _DURATIONS.validate_json(DURATIONS_PATH.read_bytes())


def _tier_for(seconds: float) -> DurationTier:
    return next((tier for limit, tier in _TIER_THRESHOLDS if seconds > limit), DurationTier.SHORT)


def _order_by_tier(items: list[pytest.Item]) -> None:
    """
    Mark each item with its duration tier, fail if the huge tier is over its cap, then run short to huge.

    A test with no recorded duration counts as short, so a new test runs early until a
    `--record-durations` run has measured it.
    """
    durations = load_durations()
    tiers = {item.nodeid: _tier_for(durations.get(item.nodeid, 0.0)) for item in items}
    for item in items:
        item.add_marker(tiers[item.nodeid].marker)
    huge = [nodeid for nodeid, tier in tiers.items() if tier is DurationTier.HUGE]
    if len(huge) > MAX_HUGE_TESTS:
        msg = (
            f"{len(huge)} tests are in the huge tier (over 60s), past the cap of {MAX_HUGE_TESTS}: "
            f"{', '.join(huge)}. Speed them up or split them."
        )
        raise pytest.UsageError(msg)
    items.sort(key=lambda item: tiers[item.nodeid])


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register `--screenshots` (`docs_screenshots_test.py`) and `--record-durations` (`DURATIONS_PATH`)."""
    parser.addoption(
        "--screenshots",
        type=ScreenshotMode,
        choices=list(ScreenshotMode),
        default=None,
        help="Run the docs-screenshot tests: `check` against, or `update`, the images in docs/images.",
    )
    parser.addoption(
        "--record-durations",
        action="store_true",
        help="Save this run's per-test durations to test-durations.json, which tiers and orders later runs.",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Start an empty per-test duration tally for this run."""
    config.stash[_MEASURED] = {}


def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> None:
    """Add each phase's time (setup, call, teardown) to the test's total, for `--record-durations`."""
    measured = item.config.stash[_MEASURED]
    measured[item.nodeid] = measured.get(item.nodeid, 0.0) + call.duration


def pytest_sessionfinish(session: pytest.Session) -> None:
    """
    Merge this run's durations into `DURATIONS_PATH`, keeping tests this run didn't run.

    A run that collected the whole suite also drops the tests that no longer exist.
    """
    if not session.config.getoption("--record-durations"):
        return
    durations = load_durations() | session.config.stash[_MEASURED]
    collected = session.config.stash.get(COLLECTED_TESTS, None)
    if collected is not None:
        durations = {nodeid: seconds for nodeid, seconds in durations.items() if nodeid in collected}
    DURATIONS_PATH.write_bytes(_DURATIONS.dump_json(dict(sorted(durations.items())), indent=2) + b"\n")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    """
    Note what was collected, then keep the docs-screenshot tests in runs of their own, and out of every other run.

    `tryfirst` so `COLLECTED_TESTS` is taken before `-k`/`-m` deselect anything. The screenshots' images depend on the
    whole database (the home page lists every project), so a run that asks for screenshots renders them in a process
    that ran nothing else.
    """
    config.stash[COLLECTED_TESTS] = (
        frozenset(item.nodeid for item in items) if _collected_whole_suite(session) else None
    )
    _order_by_tier(items)
    screenshots_requested: bool = config.getoption("--screenshots") is not None
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
        "postgres:17-alpine", username="dlboard", password="dlboard", dbname="dlboard"
    ) as pg:
        yield PostgresSettings(
            host=pg.get_container_host_ip(),
            port=pg.get_exposed_port(5432),
            database="dlboard",
            user="dlboard",
            password=SecretStr("dlboard"),
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

    bucket = "dlboard-test"
    with LocalStackContainer() as localstack:
        localstack.get_client("s3").create_bucket(
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


type MakeBlobStore = Callable[[BlobBackend, int], BlobArtifactStore]
"""What `make_blob_store` yields: build a store on a backend with a write queue of the given size."""


@pytest.fixture
def make_blob_store() -> Iterator[MakeBlobStore]:
    """
    `BlobArtifactStore.get_or_create` for one test, disposing every store it made once the test ends.

    Each store runs a write-worker process (about 115 MB). One a test forgets to dispose lives on for
    the rest of the session, and enough of them slow a whole-suite run until browser tests time out.
    """
    made: list[BlobArtifactStore] = []

    def make(backend: BlobBackend, store_q_size: int) -> BlobArtifactStore:
        made.append(BlobArtifactStore.get_or_create(backend, store_q_size))
        return made[-1]

    yield make
    for artifact_store in made:
        artifact_store.dispose()


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
def _plain_artifact_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Make artifact download URLs plain `/artifact/<id>` paths, with no running Dash app.

    `relative_path` reads Dash's process-global config, which only exists once some test has built a
    `Dash` app -- so a test that builds artifact URLs without one passes or fails by test order.
    """
    monkeypatch.setattr("dlboard.serve._backend._artifact_download.relative_path", str)


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
    """A live dlboard backend's URL, plus a store reading the same database it writes to."""

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
    A real dlboard backend (storage + REST routes, no charts) on a background thread.

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
            monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(tmp_path / "test.sqlite"))
            metadata_plugin, store = sqlite, SQLLiteStore(tmp_path / "test.sqlite")
        case StoreBackend.POSTGRES:
            settings: PostgresSettings = request.getfixturevalue("postgres_settings")
            for name, value in settings.model_dump(
                include={"host", "port", "database", "user", "db_schema"}
            ).items():
                monkeypatch.setenv(f"DLBOARD_POSTGRES_{name.upper()}", str(value))
            monkeypatch.setenv(
                "DLBOARD_POSTGRES_PASSWORD", settings.password.get_secret_value() if settings.password else ""
            )
            monkeypatch.setenv("DLBOARD_POSTGRES_SSLMODE", settings.sslmode)
            metadata_plugin, store = postgres, PostgresStore(settings)
    artifact_plugin: PluginProtocol
    match artifact_backend:
        case ArtifactBackend.FILESYSTEM:
            monkeypatch.setenv("DLBOARD_ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
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
                monkeypatch.setenv(f"DLBOARD_S3_{name.upper()}", str(value))
            monkeypatch.setenv(
                "DLBOARD_S3_SECRET_ACCESS_KEY",
                s3_config.secret_access_key.get_secret_value() if s3_config.secret_access_key else "",
            )
            download_mode = cast("S3DownloadMode", request.getfixturevalue("s3_download_mode"))
            monkeypatch.setenv("DLBOARD_S3_DOWNLOAD_MODE", download_mode.value)
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


@pytest.fixture
def telemetry(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """
    A test that switches telemetry on: set `DLBOARD_TELEMETRY_*` on the yielded `monkeypatch`.

    The per-process singletons remember what they found (including "off") for the life of the
    process, so they are closed before and after to rebuild from the test's environment.
    """
    METRICS.close()
    LOGS.close()
    TRACES.close()
    yield monkeypatch
    METRICS.close()
    LOGS.close()
    TRACES.close()


@pytest.fixture
def span_exporter(telemetry: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    """
    Tracing on in this process, with every finished span collected in memory (`flush()` makes sure it has been).

    Only this process's: a spawned one exports over OTLP to wherever `OTEL_EXPORTER_OTLP_ENDPOINT` says. Libraries
    a server instruments are un-instrumented afterwards, since that is process-wide.
    """
    exporter = InMemorySpanExporter()
    telemetry.setenv("DLBOARD_TELEMETRY_TRACES", "true")
    telemetry.setattr(_sdk, "make_exporter", lambda: exporter)
    _sdk.resource.cache_clear()  # one identity per process, remembered: a test starts over
    yield exporter
    _instrument.uninstrument_libraries()


@dataclass
class OtlpTraces:
    """An OTLP collector's view of traces: every span any process exported to it, as the wire format has them."""

    spans: list[WireSpan] = field(default_factory=list)

    def __call__(self, environ: WSGIEnvironment, start_response: StartResponse) -> list[bytes]:
        """Serve one export request: keep its spans, answer 200 to everything."""
        stream: InputStream = environ["wsgi.input"]
        body = stream.read(int(environ["CONTENT_LENGTH"]))
        if environ["PATH_INFO"] == "/v1/traces":
            for resource_spans in ExportTraceServiceRequest.FromString(body).resource_spans:
                for scope_spans in resource_spans.scope_spans:
                    self.spans.extend(scope_spans.spans)
        start_response("200 OK", [("Content-Type", "application/x-protobuf")])
        return [b""]


@pytest.fixture
def otlp_traces(telemetry: pytest.MonkeyPatch) -> Iterator[OtlpTraces]:
    """
    Tracing on, in this process and every one it spawns, exporting to a collector listening here.

    For a test that needs a spawned process's spans, which `span_exporter` cannot collect. Spans are exported every
    0.2s (the standard `OTEL_BSP_SCHEDULE_DELAY`), so a test polls for what it expects instead of waiting on a flush.
    """
    collector = OtlpTraces()
    server = make_server("127.0.0.1", 0, collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    telemetry.setenv("DLBOARD_TELEMETRY_TRACES", "true")
    telemetry.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{server.server_port}")
    telemetry.setenv("OTEL_BSP_SCHEDULE_DELAY", "200")
    yield collector
    server.shutdown()
    thread.join()
    _instrument.uninstrument_libraries()


def scraped_total(scrape: str, name: str, **labels: str) -> float:
    """The summed value of the samples called `name` in a Prometheus `scrape` whose labels include `labels`."""
    return sum(
        sample.value
        for family in text_string_to_metric_families(scrape)
        for sample in family.samples
        if sample.name == name and labels.items() <= sample.labels.items()
    )


def props(component: object) -> dict[str, Any]:
    """
    Extract a rendered Dash component's props.

    dash-mantine-components ships no py.typed marker, so its component attrs are Unknown to
    pyrefly regardless of how this is typed.
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


def find_all_props(component: Any, matches: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:  # noqa: ANN401
    """The props of every node in a dash component tree whose props `matches`, depth-first and parents first."""
    if isinstance(component, list):
        return [found for item in cast("list[Any]", component) for found in find_all_props(item, matches)]
    if not hasattr(component, "to_plotly_json"):
        return []
    component_props = props(component)
    return [
        *([component_props] if matches(component_props) else []),
        *find_all_props(component_props.get("children"), matches),
    ]


def make_fake_chart(
    chart_name: str, params_type: type[BaseModel]
) -> type[models.ChartType[typing.Any, pd.DataFrame, dict[str, object]]]:
    """
    Build a minimal registrable `ChartType` around `params_type`.

    Only the description of the settings is under test wherever this is
    used, so `render` and the `hint_required_*` methods are no-ops: just enough to satisfy
    `ChartType`'s abstract interface.
    """

    class _Fake(models.ChartType[typing.Any, pd.DataFrame, dict[str, object]], frozen=True, extra="forbid"):
        name: typing.ClassVar[str] = chart_name

        @classmethod
        @typing.override
        def parameter_type(cls) -> type[BaseModel]:
            return params_type

        @classmethod
        @typing.override
        def render(cls, parameters: BaseModel, dataframe: pd.DataFrame) -> dict[str, object]:
            return {}

        @classmethod
        @typing.override
        def hint_required_columns(cls, parameters: BaseModel) -> set[str] | None:
            return set()

        @classmethod
        @typing.override
        def hint_required_artifact_keys(cls, parameters: BaseModel) -> set[str] | None:
            return set()

        @classmethod
        @typing.override
        def hint_required_hparams(cls, parameters: BaseModel) -> set[str] | None:
            return set()

        @classmethod
        @typing.override
        def natural_width(cls, parameters: BaseModel) -> int:
            return 400

        @classmethod
        @typing.override
        def field_column_kinds(cls) -> dict[str, models.ColumnKind]:
            return {}

    return _Fake
