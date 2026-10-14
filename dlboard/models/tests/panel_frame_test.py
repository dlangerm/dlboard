"""The panel dataframe's column names, shared by the page's fetch and every chart type."""

from __future__ import annotations

import pytest

from dlboard.models import artifact_column, artifact_fname_column, artifact_keys_in, artifact_tags_column


@pytest.mark.parametrize(
    ("name", "prefix", "keys"),
    [
        pytest.param("eval/a", False, ["eval/a"], id="exact-key"),
        pytest.param("eval/", True, ["eval/a", "eval/b"], id="prefix"),
        pytest.param("", True, ["eval/a", "eval/b", "other"], id="everything"),
        pytest.param("eval", False, [], id="a-prefix-is-not-a-key"),
    ],
)
def test_artifact_keys_in_names_keys_not_their_tag_and_file_name_columns(
    name: str, *, prefix: bool, keys: list[str]
) -> None:
    columns = ["run_id", "loss"]
    for key in ("eval/a", "eval/b", "other"):
        columns += [artifact_column(key), artifact_tags_column(key), artifact_fname_column(key)]

    assert artifact_keys_in(columns, name, prefix=prefix) == keys
