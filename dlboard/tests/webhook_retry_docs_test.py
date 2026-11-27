"""How long the docs say a failing webhook is tried before it is turned off holds for the default settings (#811)."""

from __future__ import annotations

from pathlib import Path

import pendulum

from dlboard.plugins.webhooks._hooks import WebhookSettings
from dlboard.serve._custodian._supervisor import CustodianSettings

_DOCS = Path(__file__).parents[2] / "docs"


def test_a_failing_webhook_is_turned_off_after_the_time_the_docs_say() -> None:
    backoff, attempts = CustodianSettings().backoff, WebhookSettings().attempts
    waits = [backoff.after(tried) for tried in range(1, attempts)]  # between one try and the next

    total = sum(waits, pendulum.duration())

    assert round(total.total_seconds() / 3600 * 2) / 2 == 8.5
    for doc in ("configuration.md", "webhooks.md"):
        assert "about eight and a half hours" in (_DOCS / doc).read_text()
