# Background tasks

## What is this

Some work belongs to no request: purging what an admin deleted, deleting the blobs a purge orphaned, emptying the Trash, [retention](retention.md).
dlboard runs it in a **custodian**: one separate process the server starts beside its web workers, which runs every background task its plugins register.
A web worker registers tasks but never runs one, so custodial work never takes a request's thread, CPU or memory, and a worker that is recycled or crashes never takes a task with it.

## When you'd need this

You are deploying more than one server against a database, wondering why a purge has not happened, setting up an alert, or writing a plugin that has work to do in the background.

## How it works

**One custodian per server.**
The server's master process (Granian's, or the CLI's under `--debug`) starts the custodian before the workers and stops it after them.
It restarts a custodian that exits, backing off from a second to a minute, and kills and replaces one that stops sending a heartbeat for `DLBOARD_CUSTODIAN_STALL_S` (30 s), or that has not begun beating `DLBOARD_CUSTODIAN_STARTUP_S` (10 minutes) after it started (building the app runs the migrations): a hung task stalls only the custodian, which is replaced clean.
The heartbeat is kept by a thread that never touches the database, so a database that is slow, or locked by a busy web worker, is never mistaken for a hung custodian.
The custodian dies with its master (and exits by itself if its parent goes), so a `kill -9` of the server leaves nothing running, and no worker's death touches it.

**The database holds what is happening.**
Every custodian records a heartbeat, and every task its holder, last run and outcome, last error, requested run and progress, in the database all your servers share.
The admin page's **Background tasks** tab shows them (each custodian, flagged when it has missed three beats; each task, with its backlog), and `/healthz/custodian` answers 503 when no custodian has beaten in three beats or a task has gone three of its intervals without succeeding.
Alert on that rather than on `/readyz`: a server without a custodian still serves, and shouldn't be taken out of rotation for it.
The metrics `dlboard.task.runs`, `dlboard.task.duration` and `dlboard.task.backlog` (see [telemetry.md](telemetry.md#metrics)) say how each task is doing.

**Several servers share the work.**
Each server starts its own custodian, and they divide what there is to do through the database:

- A task that must run one at a time (`SINGLETON`: scheduling, say) is held under a lease of three heartbeats, renewed every heartbeat, so a custodian that dies loses it within a minute and another takes over.
  A run whose lease was taken over (its custodian stalled past it) is told to stop, `ctx.should_stop()` turning true, so a task that checks it between batches never overlaps the one that took over.
- A task whose work divides (`SHARED`: purging, retention's checks) is a queue of work items.
  Each custodian claims a small batch at a time, with `FOR UPDATE SKIP LOCKED` on Postgres, so custodians skip each other's rows instead of waiting for them, and more custodians drain more.
  A custodian that is working renews its claims every beat, so a long run is never claimed twice; an item whose custodian dies is claimed again after `DLBOARD_CUSTODIAN_CLAIM_S`, and one that fails is retried with a backoff from a minute up to a day.
- SQLite has one writer whatever the number of custodians: more servers on one SQLite database add no throughput.
  More than one server is a Postgres deployment.

**It keeps out of the way of live traffic.**
A batch of work that took `t` is followed, before the next one, by a rest of `t * (1 - d) / d`, where `d` is `DLBOARD_CUSTODIAN_DUTY_CYCLE` (0.25): a task takes at most that share of the database's time, drains as fast as that allows, and slows itself when the database is busy, because every batch then takes longer.
Nothing follows a run's last batch, so a run ends as soon as its work does.
A task claims, deletes and writes a batch at a time, never a row at a time, and an idle custodian reads the database every `DLBOARD_CUSTODIAN_POLL_S` and records its heartbeat every `DLBOARD_CUSTODIAN_BEAT_S`: a few statements a minute.

**What can go wrong, and what it looks like.**

| | |
| --- | --- |
| The custodian crashes or is killed | The server starts another within seconds (`dlboard.custodian.starts` counts it). A claimed item is claimed again once its claim runs out. |
| A task hangs | The custodian records it and exits; the server starts a new one. The task's `last_outcome` says `hung`. |
| A task keeps failing | Its error is on the tab, and the task is tried again at its next interval, with each item backing off. `/healthz/custodian` goes red after three intervals. |
| A backlog that grows | `dlboard.task.backlog` rises over several runs, and the tab shows it. Another server (on Postgres), or a higher duty cycle, drains it faster. |
| The server is stopped | Tasks are asked to stop between batches, and given `DLBOARD_WORKERS_KILL_TIMEOUT_S` before the custodian is killed. What was claimed and not finished is claimed again later. |

`DLBOARD_CUSTODIAN_ENABLED=false` starts no custodian on a server (another sharing the database may still run the tasks).
Settings are in [configuration.md](configuration.md#background-tasks).

## For plugin authors

**A task.**
Never start a thread or a process for background work; register a `Task` in `plug()`:

```python
from dlboard.serve import Task, TaskContext, TaskMode, register_task


class MyTask(StrEnum):
    TIDY = "my_tidy"


def tidy(ctx: TaskContext) -> None:
    # No user is behind this: it is not authorized, so be careful with it.
    store = get_system_data_store(ctx.app)
    while not ctx.should_stop():  # check between batches
        with ctx.batch():  # paced by the duty cycle
            ...
    ctx.report(backlog=0)  # what is left; shown on the tab and in dlboard.task.backlog


def plug(app: Dash) -> None:
    register_task(app, Task(name=MyTask.TIDY, run_once=tidy, every=pendulum.duration(minutes=5)))
```

`every` counts from when the task's last run finished, on the wall clock, as the database records it: every server and the admin page's overdue check read the same row, so a machine that sleeps runs a task that came due within a poll of waking, and several servers run an hourly task once an hour between them.
`every=None` is a task that runs only when asked for.
A task that does not finish within its `timeout` is hung; one that checks `ctx.should_stop()` between batches is told to stop at 80% of it, and goes on in its next run.
A `SHARED` task claims its work with `ctx.claim(kind, limit=...)`, then `ctx.done(items)` or `ctx.retry(item, error)`; work is queued with `get_system_task_store(app).enqueue_work([NewWorkItem(kind=..., key=...)])` (both from `dlboard.serve`), where a key is unique among queued items of its kind, so queueing twice queues once, and `request_task_run(name)` starts the task on the next poll instead of its next interval.
A request (an admin clicking Purge) may queue only a kind a plugin lets it, as a user holding a scope (`register_work_kind(app, WorkRule(...))`); everything else about the task store is the custodian's.

Tests never start a custodian: they call a task's `run_once` with a `TaskContext` (`run_task` in `dlboard/conftest.py`).

### When a run ends

There is no hook that calls a plugin when a run finishes, fails or is lost, and there won't be: a run is finished by a request in a web worker and given up as lost by the custodian, so a callback would be split between processes, slow ingest down, and miss what happened while a process was down.

**Each end, queued.**
A plugin that acts on every end, once (webhooks, `dlboard/plugins/webhooks/`), asks for a work item of its own kind per end instead: `register_run_end_work(app, kind)` in `plug()`, and a `SHARED` task that claims `kind`.
The store queues the item in the transaction that records the end, so every end that commits is queued exactly once, and one rolled back never is: no cursor to keep, and no guess at how long a slower transaction takes to commit.
An end is a run's client reporting a status the run didn't have, or the run being given up as lost; a run that reports the same end again (Lightning's `fit`, then `test`) is not queued again.
The payload is a `RunEnded` (`run_id`, `done_at`): by the time it is claimed the run may have been deleted, or heard from again, so compare its `done_at` with the run's before acting on it.
Queueing costs one row in the ending transaction per kind asked for, which is all it adds to ingest.

**Looking for them.**
A plugin that acts on runs as they are (retention, a summary once a project goes quiet) looks for them instead, in a `SINGLETON` task, as `lost_runs` (`dlboard/plugins/backend/lost_runs.py`) and retention (`dlboard/plugins/retention/`) do:

- **What ended.**
  A run is `RUNNING` until its client reports `FINISHED` or `FAILED` (a `with` block, Lightning's `finalize`), or `UNKNOWN` (its script exited without saying).
  One that goes quiet for `DLBOARD_LOST_RUN_AFTER_S` (a `kill -9`, a lost node) is marked `UNKNOWN` by `lost_runs`, with `lost_at` set; heard from again, it is `RUNNING` once more, with `lost_at` and `ended_at` cleared.
- **Where to look.**
  An experiment's `last_activity_at` moves whenever one of its runs logs, ends or is lost, so a task reads `get_runs` only for the experiments active since it last looked, and keeps when that was in a [setting](#plugin-settings-and-page-sections) of its own.
- **Not exactly once.**
  `ended_at` is the client's clock: an offline run synced later, or an import, ends in the past, and a run reported done twice (Lightning's `fit`, then `test`) moves it.
  Don't use it as a cursor.
  Keep what was handled instead, and make the work idempotent: one `NewWorkItem` per run and status, `key=f"{run.id}:{run.status}"`, done by a `SHARED` task, queues each once while it waits.

## Plugin settings and page sections

A plugin that keeps a setting of its own per project or experiment (retention's rules, say) does not add columns or methods to the store:

- `get_scoped_settings(key, project_id)` and `put_scoped_setting(key, project_id, experiment_id, value, actor)` keep a JSON value under the plugin's key, for a project or one of its experiments (`value=None` deletes it).
- `register_setting(app, SettingRule(key=..., read=..., write_project=..., write_experiment=..., check=...))` says which [role](plugins/auth.md) may read the key and write each scope (defaults: viewer reads, owner writes a project's, editor an experiment's), and may check a value before it is stored: return it as given or marked up, or raise `PermissionError`.
  A delete is checked too, with `SettingWrite.value` of `None` (return `None` to let it through).
  The authorizing store enforces this, so a plugin never authorizes by hand, and a key with no rule is the system's alone.
- `register_project_tab(app, ProjectTab(key=..., label=..., render=...))` adds a tab to a project's page, after its own Experiments, Comparisons and Sharing tabs, drawn in the page's own layout.
  `key` is its `?tab=` value, so a link opens the page on it (`/project/<id>?tab=<key>`): lowercase letters, digits and dashes, and not a key the page or another plugin already has.
  `render` returning `None` leaves the tab out for that project.
  `register_experiment_section(app, ExperimentSection(ExperimentSlot.HEADER, render))` adds a line under an experiment's header (retention says there what keeps the experiment's runs, and links to its tab).
  These are the one place a plugin reaches into a page.

- `register_run_guard(app, guard)` asks `guard(store, project_id)` before every run a request creates, once the writer's role is checked.
  `store` is the system's (no user is behind it), and the guard returns, or raises `ProjectLockedError(why)` to refuse: the REST API answers HTTP 423 with `why`, which the client puts in its error.
  Retention uses it to refuse new runs from a project far behind its rule; a request never refuses by hand.

Choosing runs by age or count, or leaving out kept ones, is `get_runs(..., created_before=, oldest_first=, include_kept=)`; `kept_runs`, `keep_runs` and `release_runs` are the generic half of keeping a run.
