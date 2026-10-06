# pyright: reportPrivateUsage=false
"""
`dlboard serve`'s guards against two ways of exposing something dangerous to the network:
`serve local` has no sign-in at all, and `--debug` runs Werkzeug's interactive debugger.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import structlog

from dlboard import _cli
from dlboard._cli import LocalServeOptions, ServerRuntimeOptions

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    """`local()` calls `_configure_logging()`, which `configure_once` only allows once per process."""
    yield
    structlog.reset_defaults()


@pytest.mark.parametrize(
    ("host", "expected"),
    [("127.0.0.1", True), ("localhost", True), ("::1", True), ("0.0.0.0", False), ("10.0.0.5", False)],
)
def test_is_loopback(host: str, expected: bool) -> None:
    assert _cli._is_loopback(host) is expected


def test_local_refuses_a_non_loopback_host_without_the_opt_in(tmp_path: Path) -> None:
    opts = LocalServeOptions(
        sqlite_location=tmp_path / "db.sqlite",
        artifact_store_location=tmp_path / "artifacts",
        runtime=ServerRuntimeOptions(host="0.0.0.0"),
    )
    with pytest.raises(SystemExit, match="no sign-in"):
        _cli.local(opts=opts)


def test_local_allows_a_non_loopback_host_with_the_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served: list[tuple[str, ServerRuntimeOptions]] = []

    def fake_serve(target: str, runtime: ServerRuntimeOptions) -> None:
        served.append((target, runtime))

    monkeypatch.setattr(_cli, "_serve", fake_serve)
    opts = LocalServeOptions(
        sqlite_location=tmp_path / "db.sqlite",
        artifact_store_location=tmp_path / "artifacts",
        i_understand_anyone_who_can_reach_this_is_an_admin=True,
        runtime=ServerRuntimeOptions(host="0.0.0.0"),
    )

    _cli.local(opts=opts)

    assert len(served) == 1


def test_local_needs_no_opt_in_for_the_loopback_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served: list[tuple[str, ServerRuntimeOptions]] = []

    def fake_serve(target: str, runtime: ServerRuntimeOptions) -> None:
        served.append((target, runtime))

    monkeypatch.setattr(_cli, "_serve", fake_serve)
    opts = LocalServeOptions(sqlite_location=tmp_path / "db.sqlite", artifact_store_location=tmp_path / "a")

    _cli.local(opts=opts)

    assert len(served) == 1


def test_debug_refuses_a_non_loopback_host() -> None:
    with pytest.raises(SystemExit, match="debugger"):
        _cli._serve("whatever:PLUGINS", ServerRuntimeOptions(debug=True, host="0.0.0.0"))


def test_debug_allows_the_loopback_default(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[dict[str, Any]] = []

    class _FakeApp:
        def run(self, **kwargs: Any) -> None:  # noqa: ANN401 -- standing in for Dash's own untyped `run(**kwargs)`
            built.append(kwargs)

    def fake_resolve_plugins(target: str) -> list[Any]:
        del target
        return []

    def fake_build_app(plugins: list[Any], *, url_prefix: str = "") -> _FakeApp:
        del plugins, url_prefix
        return _FakeApp()

    monkeypatch.setattr(_cli, "resolve_plugins", fake_resolve_plugins)
    monkeypatch.setattr(_cli, "build_app", fake_build_app)

    _cli._serve("whatever:PLUGINS", ServerRuntimeOptions(debug=True))

    assert built == [{"host": "127.0.0.1", "port": 8050, "debug": True}]
