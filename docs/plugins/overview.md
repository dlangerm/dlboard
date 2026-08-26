# Plugins

## What is this

Every piece of opinionated behavior in dltrack — where data is stored, how a chart renders, who a
request is attributed to, what pages exist, what theme is applied — is a plugin. A plugin is any
module implementing `PluginProtocol` (`dltrack/models/_plugin.py`): a single classmethod,

```python
def plug(cls, app: Dash) -> None: ...
```

`dltrack.serve.app.app(plugins)` calls `plug(app)` on every plugin in the list, in order, once at
startup. That's the entire contract — there's no base class to inherit and no registry to update
by hand, just a module with a `plug` function.

## When you'd need this

You're building a new storage backend, chart type, page, auth provider, or theme, and want to know
where the extension points are and how a plugin gets composed into a running app. Read the page for
the specific category you're building — [storage](storage.md), [charts](charts.md),
[pages](pages.md), [auth](auth.md), [themes](themes.md) — this page is about the plugin mechanism
itself, not any one category.

## How it works

**Writing one.** A plugin module needs a `plug(app: Dash) -> None` function. What it does inside
is entirely up to the category: register a Flask route, call `dash.register_page`, set a data
store on the app, register a chart type, wire a callback. Look at
`dltrack/plugins/themes/dark.py` for the smallest real example (one callback, no state) before
tackling a bigger category.

**Registering one.** Add the module to the list passed into `app()` — see `LOCAL_DEPLOYMENT` in
`dltrack/plugins/__init__.py` for the reference bundle `serve.py` uses. There's no other
registration step; a plugin not in that list is never loaded.

**Identity, not state.** Once plugged, a plugin is snapshotted as an `InstalledPlugin`
(`name` + first docstring line — see `InstalledPlugin.describe` in `dltrack/models/_plugin.py`)
for introspection (the admin page's About tab lists installed plugins this way). Deliberately no
reference to the plugin module/object itself is kept, so that introspection can't reach back into
a plugin's internal state from an arbitrary callback thread.

**Design boundary.** The core app exposes minimal hooks for plugins to use (the `Store`
accessors described in [architecture.md](../architecture.md)); any opinionated choice — what a
delete confirmation looks like, how a chart is styled, what an admin page shows — belongs in the
plugin, not the core.
