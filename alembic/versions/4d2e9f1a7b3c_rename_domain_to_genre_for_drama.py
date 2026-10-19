"""rename domain to genre for short-drama localization

Revision ID: 4d2e9f1a7b3c
Revises: 82e2a6589c16
Create Date: 2026-10-16 16:05:00.000000

Replaces the legacy, dead ``videos.domain`` column (a closed 5-value enum —
general/politics/medicine/psychology/sociology — that was never reachable from
the API or UI) with ``videos.genre``, a short-drama genre preset that biases
dialogue tone, honorifics, and terminology locking during localization.

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4d2e9f1a7b3c"
down_revision: str | None = "82e2a6589c16"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop the old domain check constraint first — it constrains the `domain`
    # column, which we rename in the next step.
    op.drop_constraint("ck_videos_domain", "videos", type_="check")

    op.alter_column(
        "videos",
        "domain",
        new_column_name="genre",
        existing_type=sa.String(32),
        existing_nullable=False,
    )

    # Genre enum check constraint. PostgreSQL has no
    # `ALTER TABLE ... ADD CONSTRAINT IF NOT EXISTS`, so use the same
    # defensive PL/pgSQL pattern as the earlier enum-constraint migration.
    _ensure_check_constraint(
        "videos",
        "ck_videos_genre",
        "genre IN ('general', 'romance', 'suspense', 'costume', "
        "'urban', 'fantasy', 'scifi')",
    )


def _ensure_check_constraint(table: str, name: str, check: str) -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = '{name}' AND conrelid = '{table}'::regclass
            ) THEN
                ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({check});
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_constraint("ck_videos_genre", "videos", type_="check")

    op.alter_column(
        "videos",
        "genre",
        new_column_name="domain",
        existing_type=sa.String(32),
        existing_nullable=False,
    )

    _ensure_check_constraint(
        "videos",
        "ck_videos_domain",
        "domain IN ('general', 'politics', 'medicine', 'psychology', 'sociology')",
    )
