"""
How a REST route reads what it was sent and answers: as the models of `dlboard._wire` and `dlboard.models`, never a dict.

A route's body is validated as the model it is (`body`), a 400 if it isn't one (`ValidationError`, see
`basic_rest_backend._ERROR_STATUSES`), and its answer is the model the client validates it back into (`answer`), so
pyrefly checks that the server answers what the client reads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from flask import Response, request
from pydantic import BaseModel

from dlboard._wire import Page

if TYPE_CHECKING:
    from collections.abc import Sequence


def body[M: BaseModel](model: type[M]) -> M:
    """The request's JSON body as `model`: read as Flask reads JSON, so a diverged loss's `NaN` comes through."""
    return model.model_validate(request.json)


def answer(model: BaseModel | None = None) -> Response:
    """`model` as the JSON a client reads it back from; `{}` for a request whose answer is only that it was done."""
    return Response("{}" if model is None else model.model_dump_json(), mimetype="application/json")


def page[T](items: Sequence[T], next_cursor: str | None) -> Response:
    """One page of a read (`Page`): its items, and the cursor of the page after it, `None` on the last."""
    return answer(Page[T](items=list(items), next_cursor=next_cursor))
