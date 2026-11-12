---
name: dlboard-plugin
description: Write a plugin for a dlboard server (a chart type, a storage backend, an auth provider, a theme, a background task, or a project setting with its own section) and compose it into the plugin list `dlboard serve custom --plugins` runs. Use when asked to connect dlboard to a database or object store, add a chart type, sign people in through an identity provider, restyle dlboard, or add a per-project setting or scheduled job to it.
---

# Writing a dlboard plugin

A dlboard server is a Dash app built at startup from a flat list of plugins. A plugin is a module with a
`plug(app: Dash) -> None` function. dlboard calls each plugin's `plug`, in list order, once for every app it builds. There
is no base class and no registry: a module is installed by being in the list, and only then.

## 1. Work against the installed version

dlboard is `0.x`, and its Python plugin API may change in any minor release. Find the version the deployment
installs, and where its source is:

```bash
python -c "import importlib.metadata as m; print(m.version('dlboard'))"
python -c "import dlboard.plugins, pathlib; print(pathlib.Path(dlboard.plugins.__file__).parent)"
```

- The built-in plugins in that directory are the reference for this version. Read the one nearest your kind (table
  below) before you write anything, and copy its shape.
- The docs for this version are at `https://github.com/dlangerm/dlboard/tree/v<version>/docs`. Start at
  `plugins/overview.md`, then the page for your kind. The published site follows `main`, which may be ahead.
- Pin `dlboard==<version>` in the deployment's requirements, so an upgrade is a change someone reviews the plugin for.

## 2. Pick the kind

| You want | In `plug` | Read first | Docs |
|---|---|---|---|
| A chart type | subclass `ChartType`, then `MyChart.register(allow_override=True)` | `dlboard/plugins/charts/series_band.py` | `plugins/charts.md` |
| A metadata store | `set_data_store`, with a `SQLStoreBase` subclass | `dlboard/plugins/data_stores/sqlite.py` | `plugins/storage.md` |
| An artifact (blob) store | `plug_blob_store`, with your own `BlobBackend` (both in `dlboard.plugins.data_stores`) | `dlboard/plugins/data_stores/s3.py` | `plugins/storage.md` |
| Sign-in through an identity provider | `set_auth_provider`, and `add_public_route` for its login and callback | `dlboard/plugins/auth/password.py` | `plugins/auth.md` |
| Colors, fonts, dark mode | `set_theme` | `dlboard/plugins/themes/default.py` | `plugins/themes.md` |
| A scheduled job | `register_task` | `dlboard/plugins/backend/trash_retention.py` | `background-tasks.md` |
| A per-project setting, shown on the project page | `register_setting` and `register_project_tab` | `dlboard/plugins/retention/` | `background-tasks.md` |

## 3. Use only the public API

A plugin may use what `dlboard.serve`, `dlboard.models` and `dlboard.plugins.data_stores` export (their `__all__`), and
nothing whose name starts with `_`. The built-ins sometimes import private modules. Copy their structure, not those imports. Besides the table above:

| Function | What for |
|---|---|
| `serve_asset` | Serve a `.js` or `.css` file kept next to your module, from this app only. |
| `add_startup_check` | Fail startup when the plugin's configuration is wrong, instead of on the first request. |
| `register_run_guard` | Refuse a run a request tries to create (a quota, say). |
| `register_experiment_section` | Add a line under an experiment's header, for a setting of your own. |
| `register_work_kind` | Let a request queue work for one of your tasks, as a user holding a scope. |
| `get_data_store` | The store in a request or callback, as the signed-in user. |
| `get_system_data_store` | The store in a task, where no user is behind the work. |
| `get_current_user` | Who is making the request. |

## 4. Compose it

Put the plugin list in a module of your own:

```python
# mydeploy/plugins.py
from dlboard.plugins import BUILTIN_BACKEND, BUILTIN_CHARTS, PASSWORD_AUTH, POSTGRES_S3_STORAGE, themes

from mydeploy import my_chart

PLUGINS = [*POSTGRES_S3_STORAGE, *PASSWORD_AUTH, *BUILTIN_BACKEND, *BUILTIN_CHARTS, my_chart, themes.default]
```

```bash
dlboard serve custom --plugins mydeploy.plugins:PLUGINS --workers 4
```

To replace a built-in, leave it out of the list and add yours. The bundles in `dlboard.plugins` are `LOCAL_STORAGE`,
`POSTGRES_STORAGE`, `POSTGRES_S3_STORAGE`, `LOCAL_AUTH`, `PASSWORD_AUTH`, `BUILTIN_BACKEND`, `BUILTIN_CHARTS`,
`LOCAL_DEPLOYMENT` and `LOCAL_DEPLOYMENT_DEFAULT` (what `dlboard serve local` runs). The pages (home, project,
experiment, admin, account) are not plugins and cannot be replaced.

## 5. Rules

- **Keep state per app, in an `AppSlot`.** Never in module globals or `app` attributes: one process can build more than
  one app, and each needs its own.
- **Register a chart type with `allow_override=True`,** for the same reason.
- **A chart type's `name` is saved in every view that uses it.** Never rename it once it is deployed: those charts stop
  rendering.
- **No threads or processes for background work.** Register a `Task`. The server's custodian process runs it, and no web
  worker does.
- **Never check permissions by hand.** `get_data_store()` only lets the signed-in user do what their roles allow, and
  `register_setting` says who may read and write your setting. Never use `get_system_data_store` in a request.
- **Routes a signed-out browser must reach** (a login form, an OIDC callback) go through `add_public_route`. Every other
  route is behind sign-in.
- **Browser JS and CSS** stay next to your module and go through `serve_asset` in `plug`. Never use a shared `assets/`
  folder or Dash's global `hooks`: both leak into every app the process builds.
- **Your settings** come from environment variables, through a `pydantic_settings.BaseSettings` with a prefix you own
  (`MYDEPLOY_`, say), read inside `plug`. Secrets go in the environment, never in code.

## 6. Test it

A chart type's methods are class methods, so test them directly. Then build an app from your plugin, which runs every
`plug` and every startup check, and request a page from it:

```python
from pathlib import Path

import pandas as pd
import pytest

from dlboard.plugins import BUILTIN_BACKEND, LOCAL_AUTH, sqlite
from dlboard.serve import app as build_app

from mydeploy import my_chart
from mydeploy.my_chart import MyChart, MyChartSettings


def test_the_chart_reads_the_metric_it_draws() -> None:
    assert MyChart.data_request(MyChartSettings(metric="loss")).metrics == frozenset({"loss"})


def test_the_chart_draws_a_logged_metric() -> None:
    frame = pd.DataFrame({"run_id": [1, 1], "step": [0, 1], "loss": [1.0, 0.5]})
    MyChart.render(MyChartSettings(metric="loss"), frame)


def test_the_app_builds_and_serves_with_the_chart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(tmp_path / "db.sqlite"))
    app = build_app([sqlite, *LOCAL_AUTH, *BUILTIN_BACKEND, my_chart])
    assert app.server.test_client().get("/").status_code == 200
```

Build test apps on `sqlite` alone. `filesystem` and `s3` start an artifact writer process for every app built, which a
test suite would leak. A storage plugin of your own goes in place of `sqlite`, pointed at a temporary location.

## 7. Check it runs

Start the deployment and open the admin page's About tab. It lists every installed plugin by module name, with the first
line of its module docstring, so give the module one.
