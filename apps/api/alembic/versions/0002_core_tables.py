"""core tables

Revision ID: 0002_core_tables
Revises: 0001_initial
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_core_tables"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "videos",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("key", sa.String(512), nullable=False, unique=True),
        sa.Column("duration_s", sa.Integer),
        sa.Column("created_at", sa.DateTime, server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.create_table(
        "renditions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("video_id", sa.String(36), nullable=False),
        sa.Column("height", sa.Integer, nullable=False),
        sa.Column("bitrate_kbps", sa.Integer),
        sa.Column("key", sa.String(512)),
        sa.Column("status", sa.String(32), server_default="queued", nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("video_id", sa.String(36), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON),
        sa.Column("status", sa.String(32), server_default="queued", nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime, server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_renditions_video_id", "renditions", ["video_id"])
    op.create_index("ix_jobs_video_id", "jobs", ["video_id"])

def downgrade():
    op.drop_index("ix_jobs_video_id", table_name="jobs")
    op.drop_index("ix_renditions_video_id", table_name="renditions")
    op.drop_table("jobs")
    op.drop_table("renditions")
    op.drop_table("videos")
