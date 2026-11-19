"""
Run tokens: an API token bound to one run, for a job to carry instead of its owner's full token (#718).

One nullable column on `ApiToken` (`NULL`: a full token, as every existing row is), owned by the run, so purging the run
takes its tokens with it: expand-only, per the policy in `docs/compatibility.md`. An older server never reads it, and
could only take a run token for a full one if it knew its prefix: it is `dlr_`, which no older server reads as a token.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op  # pyrefly: ignore [implicit-reexport]

# revision identifiers, used by Alembic.
revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_INT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    """Add the column."""
    with op.batch_alter_table("ApiToken") as tokens:
        tokens.add_column(sa.Column("run_id", _INT, nullable=True))
        # Named, as a batch (SQLite's rebuilt table) needs it to be; the models never name one, so nothing compares it.
        tokens.create_foreign_key("fk_ApiToken_run_id_Run", "Run", ["run_id"], ["id"], ondelete="CASCADE")


def downgrade() -> None:
    """Drop it again."""
    with op.batch_alter_table("ApiToken") as tokens:
        tokens.drop_constraint("fk_ApiToken_run_id_Run", type_="foreignkey")
        tokens.drop_column("run_id")
