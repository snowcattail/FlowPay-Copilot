"""Index native full text search over immutable knowledge chunks."""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade(database: str) -> None:
    _ = database
    op.execute(
        "CREATE INDEX chunks_content_fts_idx ON chunks USING gin (to_tsvector('simple', content))"
    )


def downgrade(database: str) -> None:
    _ = database
    raise RuntimeError("Destructive downgrade is intentionally unsupported; restore a backup")
