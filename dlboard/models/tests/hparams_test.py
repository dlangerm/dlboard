"""Tests for `NewHyperParams.from_raw`'s type-preservation and validation contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dlboard.models._hparams import FlatHparamDict, NewHyperParams


@pytest.mark.parametrize(
    "hparams",
    [
        {"lr": 0.1, "hidden_size": 128, "optimizer": "adam", "use_dropout": True, "seed": None},
        {"hidden_size": "128"},
    ],
)
def test_from_raw_preserves_each_value_exactly_as_given(hparams: FlatHparamDict) -> None:
    """
    dlboard never reinterprets a hyperparameter's type -- whatever the caller logs (an `int` `128`
    or the string `"128"`) is exactly what `hparams_dict` returns back, byte for byte through the
    JSON round trip. A caller that wants `128` to sort/format as a number must log it as one; a
    numeric-looking string logged on purpose (or by an upstream bug, e.g. an unparsed argparse
    `Namespace`) stays a string.
    """
    result = NewHyperParams.from_raw(run_id=1, experiment_id=1, hparams=hparams)

    assert result.hparams_dict == hparams
    for key, value in hparams.items():
        assert type(result.hparams_dict[key]) is type(value)


@pytest.mark.parametrize(
    "hparams",
    [
        {"config": {"nested": "dict"}},
        {"layers": [1, 2, 3]},
        {"tag": object()},
    ],
)
def test_from_raw_rejects_non_flat_or_non_json_scalar_values(hparams: dict[str, object]) -> None:
    """
    A hyperparameter blob must round-trip through `hparams_dict` as the flat, JSON-scalar dict its
    return type promises. Rejecting a structurally invalid value (a nested dict, a list, some other
    object) here -- right where it enters the system -- surfaces the bug loudly at the call site
    instead of letting it silently pass and confuse whatever reads it back later.

    `hparams` is deliberately mistyped here -- it's exercising the runtime check that's the actual
    backstop for a caller `from_raw`'s own static typing can't reach (e.g. `pytorch_lightning`
    handing over an arbitrary, `Any`-typed hparams object).
    """
    with pytest.raises(ValidationError):
        NewHyperParams.from_raw(
            run_id=1, experiment_id=1, hparams=hparams
        )  # pyrefly: ignore [bad-argument-type]
