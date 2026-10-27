"""Artifacts as a chart is handed them: a row per run and step, with each key's URL, tags and file name as columns."""

from __future__ import annotations

import typing

import pandas as pd

from dlboard.models import artifact_column, artifact_fname_column, artifact_tags_column
from dlboard.serve._backend._artifact_download import artifact_url

if typing.TYPE_CHECKING:
    from collections.abc import Iterable

    from dlboard.models import Artifact


def build_artifacts_dataframe(artifacts: Iterable[Artifact]) -> pd.DataFrame:
    """Pivot artifact download URLs (and tags and file names) into columns per key, indexed by (run_id, step)."""
    # A URL, not the underlying `ref` -- charts (and anything else consuming this dataframe) fetch
    # an artifact by id through `/artifact/<id>`, never by talking to the ref's backend directly.
    # One pass: each artifact is dumped and given its URL as it streams into the frame.
    df = pd.DataFrame(a.model_dump(mode="json") | {"url": artifact_url(a)} for a in artifacts)
    if df.empty:
        return df

    pivots = [
        df.pivot_table(index=["run_id", "step"], columns="key", values=value, aggfunc="first").rename(
            columns=column_name
        )
        for value, column_name in (
            ("url", artifact_column),
            ("tags", artifact_tags_column),
            ("fname", artifact_fname_column),
        )
    ]
    return pd.concat(pivots, axis=1).reset_index()
