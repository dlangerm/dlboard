"""The admin page's Retention tab: which server process enforces retention, and how every project stands against it."""

from __future__ import annotations

from typing import TYPE_CHECKING

import dash_mantine_components as dmc
import pendulum

from dlboard.serve._backend._retention_settings import OVERDUE_COUNTED, RetentionSettings
from dlboard.serve._pages._project_retention import describe

if TYPE_CHECKING:
    from datetime import datetime

    from dash.development.base_component import Component

    from dlboard import models
    from dlboard.models import DataStore


def _ago(at: datetime | None) -> str:
    if at is None:
        return "never"
    return f"{pendulum.instance(at).diff_for_humans(pendulum.now('UTC'), absolute=True)} ago"


def _worker(state: models.RetentionWorkerState) -> Component:
    if state.lease_holder is None:
        line = "Retention has not swept yet. It runs on every server process, a sweep every DLBOARD_RETENTION_INTERVAL_S."
    else:
        until = state.lease_until
        holds = (
            f", which holds the lease until {pendulum.instance(until).format('HH:mm')} UTC" if until else ""
        )
        line = f"Last swept {_ago(state.swept_at)} by {state.lease_holder}{holds}."
    since = state.trash_retention_since
    trash = (
        f" The Trash is purged of what has been in it DLBOARD_TRASH_RETENTION_DAYS, counted from {pendulum.instance(since).format('D MMM YYYY')}."
        if since is not None
        else ""
    )
    return dmc.Text(line + trash, size="sm", c="dimmed")


def _row(
    project: models.Project, retention: models.ProjectRetention, warn_after: pendulum.Duration
) -> Component:
    status = retention.status
    active, pending = retention.project.active, retention.project.pending
    overdue = status.overdue_runs if status else 0
    since = status.out_of_spec_since if status else None
    late = since is not None and pendulum.now("UTC") - pendulum.instance(since) >= warn_after
    count = f"{overdue:,}{'+' if overdue >= OVERDUE_COUNTED else ''}"
    return dmc.TableTr(
        [
            dmc.TableTd(project.name),
            dmc.TableTd(describe(active.rule) if active else "Keeps every run"),
            dmc.TableTd(
                f"{describe(pending.rule)} from {pendulum.instance(pending.effective_at).format('D MMM YYYY')}"
                if pending
                else "-",
                c="dimmed",
            ),
            dmc.TableTd(
                # Mantine fails on a colour of `null`: a project on time gets no colour at all.
                dmc.Text(count, size="sm", c="orange", fw=600) if late else dmc.Text(count, size="sm")
            ),
            dmc.TableTd(_ago(since) if since is not None else "-", c="dimmed"),
            dmc.TableTd(_ago(status.checked_at) if status else "never", c="dimmed"),
        ]
    )


def render_retention_overview(store: DataStore[...]) -> Component:
    """The worker's state, and each project's rule and standing, the furthest behind first."""
    state = store.retention_worker_state()
    warn_after = pendulum.duration(hours=RetentionSettings().retention_warn_after_hours)
    projects = [(p, store.get_retention(p.id)) for p in store.get_projects()]
    projects.sort(key=lambda item: -(item[1].status.overdue_runs if item[1].status else 0))
    return dmc.Stack(
        [
            _worker(state),
            dmc.Table(
                [
                    dmc.TableThead(
                        dmc.TableTr(
                            [
                                dmc.TableTh(h)
                                for h in (
                                    "Project",
                                    "Rule",
                                    "Pending",
                                    "Still to delete",
                                    "Behind since",
                                    "Checked",
                                )
                            ]
                        )
                    ),
                    dmc.TableTbody([_row(p, r, warn_after) for p, r in projects]),
                ],
                striped=True,
                withTableBorder=True,
                verticalSpacing="xs",
            ),
        ],
        gap="md",
    )
