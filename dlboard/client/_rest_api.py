"""
The client's HTTP transport: `BasicDlboardAPI`, built on the wire contract in `dlboard._wire`.

Pure `requests` -- no `dash`/`flask`, so importing this (and anything that imports it, like
`dlboard.client.dlboard_logger`) never needs the server stack installed.
"""

from __future__ import annotations

import itertools
import json
import warnings
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, NamedTuple, Protocol, TypeVar
from urllib.parse import urlsplit
from uuid import uuid4

import pendulum
import requests
from pydantic import AnyUrl, AwareDatetime, BaseModel, FilePath, PositiveFloat, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from dlboard import models
from dlboard._batching import Failure
from dlboard._content_encoding import ContentEncoding, compress
from dlboard._identity import resolve_username
from dlboard._telemetry import Attributes, attribute
from dlboard._telemetry.traces import SpanKind, TraceKey, carrier, span
from dlboard._version import __version__
from dlboard._wire import (
    API_VERSION,
    ARTIFACT_CONTENT_PATH,
    DLBOARD_CLIENT_VERSION_HEADER,
    DLBOARD_USER_HEADER,
    METADATA_PART_SUFFIX,
    REQUEST_ID_HEADER,
    WHOAMI_PATH,
    ArtifactInfo,
    FinishRun,
    GetOrCreateExperiment,
    GetOrCreateProject,
    Identity,
    LatestValue,
    MarkRun,
    MetricPoint,
    Page,
    Read,
    Resource,
    RunCall,
    RunToken,
    create_path,
    entity_path,
    get_or_create_path,
    read_path,
    run_path,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
    from typing import IO

    import pandas as pd


R = TypeVar("R", bound=BaseModel)
"""Pre-3.12 `TypeVar` style (the client's floor is 3.10): the response body a request validates into."""
T = TypeVar("T")
"""What a page of a read holds."""

DEFAULT_SERVER_URL: Final = "http://localhost:8050"
"""Matches `dlboard serve local`'s own default host/port (see `ServerRuntimeOptions` in `_cli.py`)."""


_TRANSIENT_CLIENT_ERRORS: Final = frozenset({HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.TOO_MANY_REQUESTS})
_ITEM_CLIENT_ERRORS: Final = frozenset(
    {
        HTTPStatus.BAD_REQUEST,
        HTTPStatus.CONFLICT,
        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
        HTTPStatus.UNPROCESSABLE_ENTITY,
    }
)
"""The 4xx a server answers about what a batch holds, so a batch without the offending item can still land."""


def classify_failure(exc: Exception) -> Failure:
    """
    What a failure to send a batch says about it (see `Failure`): a run's shipping processes, a sync and an import ask.

    Transient: the server or network is down or busy -- a connection error, a timeout, a 5xx, or a 408/429, which
    only ask to retry later. Per item: something in the batch can never be sent, so the batch is split to find it --
    a 4xx about what was sent (an invalid item, a duplicate, a batch grown past `DLBOARD_MAX_UPLOAD_MB` while the
    server was down), or an error here, such as a `File` whose path is gone. Whole: a 4xx that refuses the request
    itself, whatever is in it -- credentials, access, a run that is gone, a locked project -- so no part of it lands.
    """
    match exc:
        case requests.ConnectionError() | requests.Timeout():
            return Failure.TRANSIENT
        case requests.HTTPError(response=requests.Response() as response):
            status = response.status_code
            if status >= HTTPStatus.INTERNAL_SERVER_ERROR or status in _TRANSIENT_CLIENT_ERRORS:
                return Failure.TRANSIENT
            return Failure.PER_ITEM if status in _ITEM_CLIENT_ERRORS else Failure.WHOLE
        case _:
            return Failure.PER_ITEM


def _raise_for_status(res: requests.Response) -> None:
    """
    `res.raise_for_status()`, with what the server said and its request id in the message when it sent them.

    A bare "500 Server Error" says nothing about *which* request failed; the id is what finds that request's
    line (and traceback) in the server's log. A 4xx's `error` says why it was refused (a project that takes no
    new runs for now, say). The response stays on the error, for `classify_failure`.
    """
    try:
        res.raise_for_status()
    except requests.HTTPError as exc:
        request_id = res.headers.get(REQUEST_ID_HEADER)
        try:
            body = res.json()
        except ValueError:  # not the JSON our server's errors are: nothing more to say
            body = None
        said = body.get("error") if isinstance(body, dict) else None
        if not request_id and not said:
            raise
        msg = (
            f"{exc}"
            + (f": {said}" if said else "")
            + (f" (server request id {request_id})" if request_id else "")
        )
        raise requests.HTTPError(msg, response=res) from exc


_Method = Literal["GET", "POST"]


@dataclass(frozen=True, slots=True)
class _RequestTrace(Attributes):
    method: _Method = attribute(TraceKey.HTTP_METHOD)
    route: str = attribute(TraceKey.URL_TEMPLATE)
    host: str = attribute(TraceKey.SERVER_ADDRESS)
    port: int = attribute(TraceKey.SERVER_PORT)
    request_id: str = attribute(TraceKey.REQUEST_ID)


@dataclass(frozen=True, slots=True)
class _ResponseTrace(Attributes):
    status_code: int = attribute(TraceKey.HTTP_STATUS_CODE)


class ClientAuthSettings(BaseSettings):
    """The client's credentials, from env vars."""

    dlboard_api_key: SecretStr | None = None
    """An API token (`dlb_...`) from the server's Account page. Not needed by a server that doesn't verify identity."""


class ClientTimeoutSettings(BaseSettings):
    """
    How long any one request waits before giving up, from `DLBOARD_*` env vars.

    Without these, a hung server or a half-open TCP connection would block the training loop
    forever -- `requests` applies no timeout at all by default. Split into connect/read because a
    slow network (connect) and a slow server actually doing the write (read) call for different
    patience; both are generous defaults since the shipping loop already retries a timeout as
    transient (see `dlboard._batching.ship_batches`) rather than needing the first attempt to be fast.
    """

    model_config = SettingsConfigDict(env_prefix="DLBOARD_")

    connect_timeout_s: PositiveFloat = 10
    read_timeout_s: PositiveFloat = 60


class ClientTLSSettings(BaseSettings):
    """Which certificates the client trusts, from `DLBOARD_CA_BUNDLE`: a server with a private-CA or self-signed one."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_")

    ca_bundle: FilePath | None = None
    """
    A PEM file of the certificates to trust, instead of the system's. Unset, `requests` trusts what it does by default
    (`certifi`, or `REQUESTS_CA_BUNDLE`).
    """


_LOOPBACK_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})
_COMPRESS_FROM_BYTES: Final = 1024
"""A JSON body smaller than this (a run being created, a heartbeat) is sent as it is: there is too little to gain."""


def _is_loopback(url: str) -> bool:
    return urlsplit(url).hostname in _LOOPBACK_HOSTS


class AuthenticationFailedError(RuntimeError):
    """The server didn't accept this client's credentials (or it sent none, and the server needs some)."""


class UnsupportedServerError(RuntimeError):
    """The server speaks a REST API major this client doesn't -- see `BasicDlboardAPI.whoami`."""


class RunAPI(Protocol):
    """
    What a run says to whatever takes what it logs: a server (`BasicDlboardAPI`), or a folder (`_offline.OfflineAPI`).

    Picklable, as a run's shipping processes are handed it.
    """

    def whoami(self) -> Identity: ...
    def get_or_create_project(self, name: str, description: str = "") -> models.Project: ...
    def get_or_create_experiment(
        self, project_id: int, name: str = "default", source: models.ExperimentSource | None = None
    ) -> models.Experiment: ...
    def create_experiment(self, new_experiment: models.NewExperiment) -> models.Experiment: ...
    def create_run(self, run: models.NewRun) -> models.Run: ...
    def get_run(self, run_id: int) -> models.Run: ...
    def mark_run(
        self, run_id: int, *, pin: models.PinColor | None = None, keep: bool = False, tags: Sequence[str] = ()
    ) -> models.Run: ...
    def log_hyperparams(self, hyperparams: models.NewHyperParams) -> models.HyperParams: ...
    def record_run_context(self, context: models.NewRunContext) -> models.RunContext: ...
    def log_metric_batch(self, metrics: list[models.LoggedMetrics]) -> None: ...
    def log_artifact_batch(self, artifacts: Iterable[tuple[models.NewArtifact, Path | AnyUrl]]) -> None: ...
    def finish_run(
        self, run_id: int, status: models.RunStatus, ended_at: AwareDatetime | None = None
    ) -> None: ...
    def heartbeat(self, run_id: int) -> bool: ...


class BasicDlboardAPI:
    """What a run says to a dlboard server, over its REST API, and what a script reads back from one."""

    def __init__(self, base_url: str = DEFAULT_SERVER_URL, api_key: SecretStr | None = None) -> None:
        """Initialize the API class, authenticating with `api_key` (default: the `DLBOARD_API_KEY` env var)."""
        self.base_url = base_url
        api_key = api_key or ClientAuthSettings().dlboard_api_key
        # Resolved once per process (not per call): who's actually running this is not going to
        # change mid-run, and every request this client makes should be attributed consistently.
        # The username header only matters to a server that doesn't verify identity; one that does
        # goes by the API key alone.
        self._headers = {
            DLBOARD_USER_HEADER: resolve_username(),
            DLBOARD_CLIENT_VERSION_HEADER: __version__,
        }
        if api_key is not None:
            self._headers["Authorization"] = f"Bearer {api_key.get_secret_value()}"
            if urlsplit(base_url).scheme == "http" and not _is_loopback(base_url):
                warnings.warn(
                    f"Sending an API key to {base_url} over plain HTTP -- anyone on the network path "
                    "can read it. Use an https:// URL, or keep this server on localhost/a private network.",
                    stacklevel=2,
                )
        # One `Session` per client, reused across every request this process makes: `requests`
        # otherwise opens a fresh TCP/TLS connection per call, which adds up over a training run's
        # worth of metric/artifact batches.
        self._session = requests.Session()
        timeouts = ClientTimeoutSettings()
        self._timeout = (timeouts.connect_timeout_s, timeouts.read_timeout_s)
        # Given to every request, not set on the session: `requests` lets `REQUESTS_CA_BUNDLE` override a session's.
        ca_bundle = ClientTLSSettings().ca_bundle
        self._verify: str | bool = str(ca_bundle) if ca_bundle is not None else True
        # Whether a JSON body is compressed: once `whoami` has heard the server takes zstd, before a run's shipping
        # processes are handed a copy of this client. Plain until then, as an older server needs.
        self._compresses = False

    def _send(  # noqa: PLR0913 -- the parts of a request, each optional but the method and route
        self,
        method: _Method,
        route: str,
        *,
        path: str | None = None,
        body: BaseModel | Sequence[BaseModel] | None = None,
        files: Iterable[tuple[str, tuple[str, IO[bytes] | str, str]]] | None = None,
        params: Mapping[str, str | Sequence[str]] | None = None,
    ) -> requests.Response:
        """
        Send one request, as a client span that the server's span for it continues.

        `route` is the path as a template (`api/v1/runs/<int:entity_id>`), which names the span -- a name with an
        id in it would be one of a million. `path` is the concrete one to request, when it differs. Every request
        carries an `X-Request-Id` of its own, known here even if no response ever comes back to say it, and the
        trace context on its way to the server. Never a credential: `Authorization` is not an attribute.

        `body` is sent as JSON with NaN and the infinities spelled out (`NaN`, `Infinity`), which the server reads back:
        a loss that diverged is worth recording. `requests`' own `json=` refuses them, which would leave a batch that
        holds one failing for good. It is compressed, unless it is too small to gain from it, once `whoami` has said how.
        """
        match body:
            case None:
                data = None
            case BaseModel():
                data = json.dumps(body.model_dump(mode="json")).encode()
            case _:
                data = json.dumps([model.model_dump(mode="json") for model in body]).encode()
        content_type: dict[str, str] = {} if data is None else {"Content-Type": "application/json"}
        if data is not None and self._compresses and len(data) >= _COMPRESS_FROM_BYTES:
            data, content_type["Content-Encoding"] = compress(data), ContentEncoding.ZSTD
        request_id = uuid4().hex
        server = urlsplit(self.base_url)
        host = server.hostname or ""
        port = server.port or (443 if server.scheme == "https" else 80)
        attributes = _RequestTrace(method=method, route=route, request_id=request_id, host=host, port=port)
        with span(f"{method} /{route}", SpanKind.CLIENT, attributes) as active:
            res = self._session.request(
                method,
                f"{self.base_url}/{path or route}",
                data=data,
                files=files,
                params=params,
                headers={**self._headers, **content_type, REQUEST_ID_HEADER: request_id, **carrier()},
                timeout=self._timeout,
                verify=self._verify,
            )
            active.set(_ResponseTrace(status_code=res.status_code))
            if not res.ok:  # the caller decides whether this status is an error; the span records that it was
                active.failed(requests.HTTPError(f"{res.status_code} {res.reason}", response=res))
        return res

    def _post(self, route: str, body: BaseModel, return_model: type[R], *, path: str | None = None) -> R:
        res = self._send("POST", route, path=path, body=body)
        _raise_for_status(res)
        return return_model.model_validate(res.json())

    def whoami(self) -> Identity:
        """
        Who the server authenticates this client as, and the version/API handshake.

        Raises `AuthenticationFailedError` if the server rejected this client's credentials, or
        `UnsupportedServerError` if it speaks a REST API major this client doesn't -- both meant to
        fail fast, before `DLBoardLogger.__init__` spawns the shipping processes that would
        otherwise just silently drop every batch against a server like that.
        """
        res = self._send("GET", WHOAMI_PATH)
        if res.status_code == HTTPStatus.UNAUTHORIZED:
            msg = (
                f"The dlboard server at {self.base_url} rejected this client's credentials. Create an API "
                "token from your Account page and set it as DLBOARD_API_KEY (or pass `api_key`)."
            )
            raise AuthenticationFailedError(msg)
        _raise_for_status(res)
        identity = Identity.model_validate(res.json())
        if identity.api_version != API_VERSION:
            msg = (
                f"The dlboard server at {self.base_url} speaks REST API v{identity.api_version} "
                f"(server version {identity.server_version}), but this client ({__version__}) speaks "
                f"v{API_VERSION}. Install a matching `dlboard` version."
            )
            raise UnsupportedServerError(msg)
        self._compresses = ContentEncoding.ZSTD in identity.accepted_encodings
        return identity

    def create_project(self, new_project: models.NewProject) -> models.Project:
        """Create a new project."""
        return self._post(create_path(Resource.PROJECTS), new_project, models.Project)

    def get_or_create_project(self, name: str, description: str = "") -> models.Project:
        """Get the project named `name`, creating it (with `description`) if it doesn't exist yet."""
        return self._post(
            get_or_create_path(Resource.PROJECTS),
            GetOrCreateProject(name=name, description=description),
            models.Project,
        )

    def create_experiment(self, new_experiment: models.NewExperiment) -> models.Experiment:
        """Create a new experiment."""
        return self._post(create_path(Resource.EXPERIMENTS), new_experiment, models.Experiment)

    def get_or_create_experiment(
        self, project_id: int, name: str = "default", source: models.ExperimentSource | None = None
    ) -> models.Experiment:
        """Get the named experiment within `project_id`, creating it if it doesn't exist yet."""
        return self._post(
            get_or_create_path(Resource.EXPERIMENTS),
            GetOrCreateExperiment(project_id=project_id, name=name, source=source),
            models.Experiment,
        )

    def create_run(self, run: models.NewRun) -> models.Run:
        """Initialize a new run."""
        return self._post(create_path(Resource.RUNS), run, models.Run)

    def get_run(self, run_id: int) -> models.Run:
        """The existing run `run_id`. Raises `requests.HTTPError` (404) if it's missing, deleted, or not yours to see."""
        res = self._send("GET", entity_path(Resource.RUNS), path=entity_path(Resource.RUNS, str(run_id)))
        _raise_for_status(res)
        return models.Run.model_validate(res.json())

    def finish_run(
        self, run_id: int, status: models.RunStatus, ended_at: AwareDatetime | None = None
    ) -> None:
        """
        Report that run `run_id` is done, as `status`, ending at `ended_at` -- by default now, by this machine's clock.

        A server from before this existed answers 404, as does a run that is gone or not yours: both
        mean there is nothing to record, so neither is an error. Anything else raises.
        """
        body = FinishRun(status=status, ended_at=ended_at or pendulum.now(pendulum.UTC))
        try:
            self._post(run_path(RunCall.FINISH), body, models.Run, path=run_path(RunCall.FINISH, str(run_id)))
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != HTTPStatus.NOT_FOUND:
                raise

    def heartbeat(self, run_id: int) -> bool:
        """
        Tell the server run `run_id` is still alive; whether it is listening.

        `False` for a 404 -- a server from before heartbeats, or a run that is gone -- after which there is no point in
        another. Anything else raises.
        """
        response = self._send(
            "POST", run_path(RunCall.HEARTBEAT), path=run_path(RunCall.HEARTBEAT, str(run_id))
        )
        if response.status_code == HTTPStatus.NOT_FOUND:
            return False
        _raise_for_status(response)
        return True

    def run_token(self, run_id: int) -> RunToken:
        """
        A run token for run `run_id`: what a job that logs to the run carries as its `DLBOARD_API_KEY`, instead of yours.

        It logs to that run alone (a run started with it is that run), reaches nothing else, and expires (the server's
        `DLBOARD_RUN_TOKEN_LIFETIME_HOURS`, a week by default). Asking takes an editor of the run's project, and a full
        token or session, not a run token. Raises `requests.HTTPError` when the server refuses (a 404 from one from before
        run tokens).
        """
        response = self._send("POST", run_path(RunCall.TOKEN), path=run_path(RunCall.TOKEN, str(run_id)))
        _raise_for_status(response)
        return RunToken.model_validate(response.json())

    def mark_run(
        self, run_id: int, *, pin: models.PinColor | None = None, keep: bool = False, tags: Sequence[str] = ()
    ) -> models.Run:
        """
        Pin run `run_id` as this client's user in the colour `pin`, keep it from retention (`keep`), and/or tag it (`tags`).

        Keeping takes an editor of the run's project. Raises `requests.HTTPError` when the server refuses it (a 403, or a
        404 for a run that is gone, not yours, or a server from before this existed), with why in its message.
        """
        return self._post(
            run_path(RunCall.MARK),
            MarkRun(pin=pin, keep=keep, tags=list(tags)),
            models.Run,
            path=run_path(RunCall.MARK, str(run_id)),
        )

    def log_hyperparams(self, hyperparams: models.NewHyperParams) -> models.HyperParams:
        """Log hyperparameters."""
        return self._post(create_path(Resource.HYPERPARAMS), hyperparams, models.HyperParams)

    def record_run_context(self, context: models.NewRunContext) -> models.RunContext:
        """Record what produced a run; a run keeps the first context recorded for it, which is returned."""
        return self._post(create_path(Resource.RUN_CONTEXTS), context, models.RunContext)

    def log_metric_batch(self, metrics: list[models.LoggedMetrics]) -> None:
        """Log a batch of metrics."""
        _raise_for_status(self._send("POST", create_path(Resource.METRICS), body=metrics))

    def log_artifact_batch(self, artifacts: Iterable[tuple[models.NewArtifact, Path | AnyUrl]]) -> None:
        """
        Log a batch of artifacts in one request, split into an upload batch and a link batch.

        Each artifact is paired with either a local file to upload (written by its own
        `to_artifact`, e.g. `client.artifacts.image.Image`) or an already-stored ref to link
        (`client.artifacts.link.Link`) -- see `AnyArtifact.to_artifact`.

        An artifact under an overlay key (`overlay_key`) that is neither a layer (`LAYER_KIND_TAG`) nor an image would be
        taken by the image chart for an overlay of the image it names, but cannot be drawn; that is warned about
        (once per key, as `warnings` shows a message once).
        """
        uploads: list[tuple[models.NewArtifact, Path]] = []
        links: list[tuple[models.NewArtifact, AnyUrl]] = []
        for artifact, source in artifacts:
            if (
                models.OverlayKey.parse(artifact.key) is not None
                and models.LAYER_KIND_TAG not in artifact.tags
                and not models.is_inlineable_artifact(artifact.fname)
            ):
                warnings.warn(
                    f"The artifact key {artifact.key!r} contains {models.OVERLAY_KEY_SUFFIX!r}, which an image chart "
                    "reads as an overlay of the image logged under the key before it, but this artifact is neither "
                    "an Overlay nor an image, so it cannot be drawn over one. Log it under another key.",
                    stacklevel=2,
                )
            match source:
                case Path():
                    uploads.append((artifact, source))
                case AnyUrl():
                    links.append((artifact, source))
        if uploads:
            self._upload_artifacts(uploads)
        if links:
            self._link_artifacts(links)

    def _upload_artifacts(self, uploads: list[tuple[models.NewArtifact, Path]]) -> None:
        """
        Upload a batch of artifacts in one request.

        Every artifact travels as two multipart parts named by its position in the batch -- its
        file (`0`) and its metadata (`0.json`) -- never by its key: many artifacts routinely share
        a key (one image per step), and the server matches metadata to file by part name.
        """
        # `requests` never closes the file handles it's handed -- opened explicitly (not inline
        # in the `files=` generator below) so they can be closed in `finally` regardless of
        # whether the request succeeds, rather than leaking a descriptor per artifact.
        opened = [path.open("rb") for _, path in uploads]
        try:
            res = self._send(
                "POST",
                create_path(Resource.ARTIFACTS),
                files=itertools.chain(
                    *(
                        (
                            (str(i), (a.fname, fh, "application/octet")),
                            (
                                f"{i}{METADATA_PART_SUFFIX}",
                                (a.fname, a.model_dump_json(), "application/json"),
                            ),
                        )
                        for i, ((a, _path), fh) in enumerate(zip(uploads, opened, strict=True))
                    )
                ),
            )
            _raise_for_status(res)
        finally:
            for fh in opened:
                fh.close()

    def _link_artifacts(self, links: list[tuple[models.NewArtifact, AnyUrl]]) -> None:
        """Register a batch of already-stored artifacts by ref, with no bytes uploaded."""
        body = [models.NewArtifactLink.model_validate(a.model_dump() | {"ref": ref}) for a, ref in links]
        _raise_for_status(self._send("POST", create_path(Resource.ARTIFACT_LINKS), body=body))

    # -- Reading back what was logged, a page at a time ---------------------------------------------------------------
    #
    # Each read is one page: at most `limit` items (and never more than the server's own cap for that read), and the
    # cursor that asks for the next page, `None` on the last. `pages` walks them lazily, as `iter_runs`/`iter_series` do.

    def _read(
        self, route: str, path: str, parse: Callable[[Any], T], query: Mapping[str, str | Sequence[str]]
    ) -> Page[T]:
        res = self._send("GET", route, path=path, params=query)
        _raise_for_status(res)
        page = Page[Any].model_validate(res.json())
        return Page(items=[parse(item) for item in page.items], next_cursor=page.next_cursor)

    def projects(
        self, *, search: str = "", cursor: str | None = None, limit: int | None = None
    ) -> Page[models.Project]:
        """A page of the projects you can see, in the order they were made; `search` keeps those whose name has it."""
        route = create_path(Resource.PROJECTS)
        return self._read(
            route, route, models.Project.model_validate, _paging(cursor, limit, {"search": search})
        )

    def experiments(
        self, project_id: int, *, search: str = "", cursor: str | None = None, limit: int | None = None
    ) -> Page[models.Experiment]:
        """A page of a project's experiments, newest first; `search` keeps those whose name has it."""
        return self._read(
            read_path(Resource.PROJECTS, Read.EXPERIMENTS),
            read_path(Resource.PROJECTS, Read.EXPERIMENTS, str(project_id)),
            models.Experiment.model_validate,
            _paging(cursor, limit, {"search": search}),
        )

    def runs(
        self,
        experiment_id: int,
        *,
        search: str = "",
        tags: Sequence[str] = (),
        cursor: str | None = None,
        limit: int | None = None,
    ) -> Page[models.Run]:
        """A page of an experiment's runs, newest first: `search` by name (or id), `tags` the runs with any of them."""
        return self._read(
            read_path(Resource.EXPERIMENTS, Read.RUNS),
            read_path(Resource.EXPERIMENTS, Read.RUNS, str(experiment_id)),
            models.Run.model_validate,
            _paging(cursor, limit, {"search": search, "tag": list(tags)}),
        )

    def metric_keys(
        self, experiment_id: int, *, cursor: str | None = None, limit: int | None = None
    ) -> Page[str]:
        """A page of the keys an experiment's runs logged metrics under, in order."""
        return self._read(
            read_path(Resource.EXPERIMENTS, Read.METRIC_KEYS),
            read_path(Resource.EXPERIMENTS, Read.METRIC_KEYS, str(experiment_id)),
            str,
            _paging(cursor, limit),
        )

    def artifact_keys(
        self, experiment_id: int, *, cursor: str | None = None, limit: int | None = None
    ) -> Page[str]:
        """A page of the keys an experiment's runs logged artifacts under, in order."""
        return self._read(
            read_path(Resource.EXPERIMENTS, Read.ARTIFACT_KEYS),
            read_path(Resource.EXPERIMENTS, Read.ARTIFACT_KEYS, str(experiment_id)),
            str,
            _paging(cursor, limit),
        )

    def hyperparams(self, run_id: int) -> dict[str, Any]:
        """A run's hyperparameters, flattened as it logged them: empty if it logged none."""
        res = self._send(
            "GET",
            read_path(Resource.RUNS, Read.HYPERPARAMS),
            path=read_path(Resource.RUNS, Read.HYPERPARAMS, str(run_id)),
        )
        _raise_for_status(res)
        return dict(res.json())

    def summary(
        self, run_id: int, *, cursor: str | None = None, limit: int | None = None
    ) -> Page[LatestValue]:
        """A run's latest value under each of a page of its experiment's metric keys (those it logged nothing under left out)."""
        return self._read(
            read_path(Resource.RUNS, Read.SUMMARY),
            read_path(Resource.RUNS, Read.SUMMARY, str(run_id)),
            LatestValue.model_validate,
            _paging(cursor, limit),
        )

    def series(
        self, run_id: int, key: str, *, cursor: str | None = None, limit: int | None = None
    ) -> Page[MetricPoint]:
        """A page of every value a run logged under `key`, by step (a step logged twice is there twice, in that order)."""
        return self._read(
            read_path(Resource.RUNS, Read.SERIES),
            read_path(Resource.RUNS, Read.SERIES, str(run_id)),
            MetricPoint.model_validate,
            _paging(cursor, limit, {"key": key}),
        )

    def artifacts(
        self, run_id: int, key: str, *, cursor: str | None = None, limit: int | None = None
    ) -> Page[ArtifactInfo]:
        """A page of a run's artifacts under `key`, without their bytes: by step, then those logged at no step."""
        return self._read(
            read_path(Resource.RUNS, Read.ARTIFACTS),
            read_path(Resource.RUNS, Read.ARTIFACTS, str(run_id)),
            ArtifactInfo.model_validate,
            _paging(cursor, limit, {"key": key}),
        )

    def artifact_bytes(self, artifact_id: int) -> bytes:
        """An artifact's bytes, by its id (`ArtifactInfo.id`), from wherever the server keeps them."""
        res = self._send(
            "GET",
            ARTIFACT_CONTENT_PATH.format(artifact_id="<int:artifact_id>"),
            path=ARTIFACT_CONTENT_PATH.format(artifact_id=artifact_id),
        )
        _raise_for_status(res)
        return res.content

    def iter_runs(
        self, experiment_id: int, *, search: str = "", tags: Sequence[str] = ()
    ) -> Iterator[Page[models.Run]]:
        """An experiment's runs, a page at a time, newest first, read as each page is asked for."""
        return pages(lambda cursor: self.runs(experiment_id, search=search, tags=tags, cursor=cursor))

    def iter_series(self, run_id: int, key: str) -> Iterator[Page[MetricPoint]]:
        """What a run logged under `key`, a page at a time, read as each page is asked for: `for page in api.iter_series(...)`."""
        return pages(lambda cursor: self.series(run_id, key, cursor=cursor))

    def series_frame(self, run_id: int, key: str, *, max_rows: int) -> SeriesFrame:
        """
        What a run logged under `key` as a pandas `DataFrame` (`step`, `value`, `timestamp_utc`), of at most `max_rows` rows.

        Read page by page until the series ends or `max_rows` is reached, whichever is first: `complete` says which, so a
        frame that stopped short is never mistaken for the whole series. Needs pandas installed (it is not a dependency).
        """
        import pandas as pd  # optional: only this needs it

        rows: list[MetricPoint] = []
        complete = True
        for page in self.iter_series(run_id, key):
            rows.extend(page.items)
            if len(rows) >= max_rows and (page.next_cursor is not None or len(rows) > max_rows):
                complete = False
                break
        frame = pd.DataFrame(
            [point.model_dump() for point in rows[:max_rows]], columns=["step", "value", "timestamp_utc"]
        )
        return SeriesFrame(frame=frame, complete=complete)


class SeriesFrame(NamedTuple):
    """A series read into pandas (`BasicDlboardAPI.series_frame`), and whether it is all of it."""

    frame: pd.DataFrame
    complete: bool
    """`False` if the series went on past the rows asked for."""


def pages(read: Callable[[str | None], Page[T]]) -> Iterator[Page[T]]:
    """Every page of a read, each one read only when it is asked for: `read(cursor)` is one page, from `None` on."""
    cursor: str | None = None
    while True:
        page = read(cursor)
        yield page
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


def _paging(
    cursor: str | None, limit: int | None, filters: Mapping[str, str | Sequence[str]] | None = None
) -> dict[str, str | Sequence[str]]:
    """A read's query: what it keeps (`filters`), where its page starts (`cursor`) and how many it holds (`limit`)."""
    return {
        **(filters or {}),
        **({} if cursor is None else {"cursor": cursor}),
        **({} if limit is None else {"limit": str(limit)}),
    }
