# Retention

A deployment that people log to for months grows without bound unless something deletes old runs. Retention does: each
project keeps a number of runs, or runs of a certain age, and a background worker moves the rest to the Trash, then purges
the Trash of what has been in it long enough.

![A project's Retention section: the rule in effect, a change waiting out its grace period, and the form](images/retention.png)

## Rules

A project has one rule, set in the **Retention** section of its page:

- **Every run**: nothing is deleted. A project that existed before retention keeps this until an admin sets a default.
- **The newest runs**: keep the newest N runs **of each experiment**, or N **of the whole project** together.
- **Runs newer than**: keep the runs created in the last N days.

An experiment may have a stricter rule of its own, of the same kind: fewer runs, or fewer days. Its project's rule is the
most it can keep. A project that keeps every run lets its experiments choose any kind.

Changing the *kind* of a project's rule (from a count to an age, say) drops every one of its experiments' rules: a number of
runs says nothing about a number of days.

Who sets what:

| | Can |
|---|---|
| owner | set the project's rule, cancel its pending change |
| editor | set (or cancel) an experiment's rule |
| viewer | see the rules and how the project stands |

An admin can do all of it, in every project.

## When a rule takes effect

A changed rule waits `DLBOARD_RETENTION_GRACE_DAYS` (7) before it takes effect, and the section shows it as pending, with
the date and how many runs it would delete as things stand. Until then **the rule before it is still enforced**: saving
again replaces the pending change, but never puts retention off. Saving the rule in effect again, or **Cancel pending
change** (for the project or experiment the form is set to), drops the pending change.

A new project starts on the server's default (below) at once: it has nothing to delete yet.

## What is deleted

Retention deletes in batches, not one run for each new one. A count acts once a project or experiment holds
`DLBOARD_RETENTION_RUN_SLACK` (10) runs past it, and then deletes down to the count; an age acts once a run is
`DLBOARD_RETENTION_AGE_SLACK_DAYS` (1) past it, and then deletes every run older than the age. The oldest go first.

Never deleted by retention:

- **A kept run.** Select runs in any run list and click **Keep** in the bar: a lock after a run's name says it is kept. A
  kept run is not counted either, so keeping runs never makes retention delete others. An editor keeps a run; only an owner
  (or an admin) releases it (**Release**) or deletes it, and deleting an experiment that holds a kept run is an owner's too.
  Keeping is not pinning: anyone can pin a run, which flags it for a comparison and nothing more.
- **A run still running**, for `DLBOARD_RETENTION_RUNNING_GRACE_DAYS` (7). A run that has been running for longer than that
  has lost its client (a training process that died never says it ended) and is deleted as any other.

What retention deletes goes to the **Trash**, as a run you delete does, and can be restored from the admin page until it is
purged. The audit log names the retention user (`dlboard:retention`) as who deleted it.

## The Trash

The worker purges anything that has been in the Trash `DLBOARD_TRASH_RETENTION_DAYS` (30), whoever deleted it: a purge is
for good, with the artifacts' files. The 30 days count from no earlier than the worker's first sweep, so upgrading never
purges what was already in the Trash the moment the server starts.

## The worker

Every server process runs the retention worker, a sweep every `DLBOARD_RETENTION_INTERVAL_S` (300), but only the one
holding the retention lease in the database does anything, so Granian's workers and replicas sharing a database never
contend for the same rows. A process that dies holds the lease until it runs out, and another takes over.

A sweep deletes `DLBOARD_RETENTION_BATCH` (100) runs to a transaction, pausing `DLBOARD_RETENTION_PAUSE_S` (1 s) between
batches so live traffic gets the database back, and at most `DLBOARD_RETENTION_SWEEP_BUDGET` (5,000) a sweep. A project far
past its rule (one whose rule just got much stricter) is caught up over several sweeps. Until it is, the project page says
how many runs it still holds to delete, and after `DLBOARD_RETENTION_WARN_AFTER_HOURS` (24) it warns; the admin page's
**Retention** tab lists every project, the furthest behind first. Raise the budget if a project stays behind.

`DLBOARD_RETENTION_ENABLED=false` turns the worker off: rules are still kept and shown, and nothing is deleted.

## For admins: defaults and caps

| Variable | |
|---|---|
| `DLBOARD_RETENTION_DEFAULT_MAX_RUNS`, `DLBOARD_RETENTION_DEFAULT_UNIT` | A new project keeps this many runs of each experiment (or of the project). |
| `DLBOARD_RETENTION_DEFAULT_MAX_AGE_DAYS` | A new project keeps runs this many days old. Not with the one above. |
| `DLBOARD_RETENTION_MAX_RUNS`, `DLBOARD_RETENTION_MAX_AGE_DAYS` | The most an owner may keep. With a cap set, an owner can no longer keep every run, nor choose a kind of rule that has no cap; only an admin goes past it, and the rule says that one did. |

A project that existed before retention gets the default when the worker first sweeps, after the grace period like any
change. Lowering a cap does the same to every project past it, except one an admin set past it on purpose. Changing the
default later changes no existing project: each keeps the rule it has.

The counted runs are a project's own, so a cap of 1,000 runs is per experiment: a project of ten experiments can keep
10,000. Every variable is in [configuration.md](configuration.md#retention); the metrics are in
[telemetry.md](telemetry.md#metrics).

## Not yet

Retention warns when it falls behind, but nothing yet refuses new data from a project that is far past its rule
(a read-only mode, to stop a project being filled faster than it is drained). Keeping a run from the client, as it is
created, is not there yet either.
