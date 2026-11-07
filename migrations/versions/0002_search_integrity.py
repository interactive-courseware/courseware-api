"""Add deferred cross-table current-version constraint and catalog indexes."""

from alembic import op

revision = "0002_search_integrity"
down_revision = "6413f6e52bed"
branch_labels = None
depends_on = None


def upgrade():
    op.create_foreign_key(
        "fk_current_course_release", "courses", "releases", ["id", "current_release_id"], ["course_id", "id"]
    )
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_index(
        "ix_metadata_search_trgm",
        "course_metadata",
        ["search_text"],
        postgresql_using="gin",
        postgresql_ops={"search_text": "gin_trgm_ops"},
    )
    op.create_index("ix_metadata_tags", "course_metadata", ["tags"], postgresql_using="gin")


def downgrade():
    op.drop_index("ix_metadata_tags", table_name="course_metadata")
    op.drop_index("ix_metadata_search_trgm", table_name="course_metadata")
    op.drop_constraint("fk_current_course_release", "courses", type_="foreignkey")
