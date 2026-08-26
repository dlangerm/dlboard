# Page plugins

## What is this

A page plugin fills in the content of one of the app's routed pages. The routes themselves —
`/`, `/project/<id>`, `/experiment/<id>`, `/admin` — are registered once, in core
(`dltrack/serve/_pages/*.py`, via Dash's `use_pages` file-based routing), each as an empty
container `Div` keyed by a shared id from `dltrack/models/constants.py`
(`PAGE_HOME_ID`, `PAGE_PROJECT_ID`, `PAGE_EXPERIMENT_ID`, `PAGE_ADMIN_ID`). A page plugin owns what
actually renders inside one of those containers: `dltrack/plugins/pages/simple_homepage.py`,
`simple_project_page.py`, `simple_experiment_page.py`, `simple_admin_page.py` are the four
built-ins `BUILTIN_PAGES` wires up.

## When you'd need this

You want a materially different homepage, project view, experiment view, or admin page — a
different layout, different affordances, integration with something the built-in version doesn't
do. Swap the corresponding `simple_*_page` module out for yours in the plugin list passed to
`app()`.

## How do I build one

`plug(app)` registers a callback whose `Output` is that page's container id, plus whatever other
callbacks the page's interactivity needs:

```python
def plug(app: Dash) -> None:
    @app.callback(Output(constants.PAGE_HOME_ID, component_property="children"))
    def layout_homepage() -> dmc.Container:
        ...  # build and return the page

    # further callbacks for buttons, forms, etc. inside that layout
```

`simple_homepage.py` is the smallest real example (project list + a "create project" form) —
start there, not `simple_experiment_page.py` (2000+ lines; it's the biggest page in the app by a
wide margin, because the experiment view has the most interactive surface: panels, edit mode,
chart settings, the hparam table).

Reach shared state the same way every other plugin does — `get_data_store()`,
`get_artifact_store()`, `get_current_user()` (`dltrack/serve/_backend/`) — never by importing
another page plugin's internals directly. Reusable sub-widgets that aren't specific to one page
(a delete-confirm button+modal, a description editor) live in `dltrack/plugins/pages/_*.py`
private helper modules and get imported by whichever page needs them.
