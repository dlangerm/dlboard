"""A value bound into a failing statement never reaches an error's text, so never a log line or a trace's span."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa

from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


@pytest.mark.parametrize("store_backend", EVERY_STORE_BACKEND)
def test_a_failing_statement_does_not_carry_the_values_bound_into_it(
    store: SQLLiteStore, store_backend: StoreBackend
) -> None:
    del store_backend  # only selects which database `store` runs on
    secret = "-".join(["hunter2", "the", "password"])

    with pytest.raises(sa.exc.DBAPIError) as caught, store._engine.connect() as connection:
        connection.execute(
            sa.text("select * from a_table_that_is_not_there where name = :name"), {"name": secret}
        )

    assert secret not in str(caught.value)
