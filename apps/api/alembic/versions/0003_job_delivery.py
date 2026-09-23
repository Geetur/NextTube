"""durable job delivery

Revision ID: 0003_job_delivery
Revises: 0002_core_tables
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_job_delivery"
down_revision = "0002_core_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "jobs",
        sa.Column("last_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column("started_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_check_constraint(
        "ck_jobs_status",
        "jobs",
        "status IN ('queued', 'running', 'retry_wait', 'done', 'failed')",
    )
    op.create_index("ix_jobs_status_lease", "jobs", ["status", "lease_expires_at"])

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("job_id", sa.String(length=36), nullable=True),
        sa.Column("topic", sa.String(length=255), nullable=False),
        sa.Column("message_key", sa.String(length=255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column(
            "available_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("claimed_by", sa.String(length=128), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(), nullable=True),
        sa.Column("publish_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("state IN ('pending', 'publishing', 'published')", name="ck_outbox_state"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_outbox_events_job_id", "outbox_events", ["job_id"])
    op.create_index(
        "ix_outbox_events_delivery",
        "outbox_events",
        ["state", "available_at", "claim_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_events_delivery", table_name="outbox_events")
    op.drop_index("ix_outbox_events_job_id", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index("ix_jobs_status_lease", table_name="jobs")
    op.drop_constraint("ck_jobs_status", "jobs", type_="check")
    op.drop_column("jobs", "finished_at")
    op.drop_column("jobs", "started_at")
    op.drop_column("jobs", "lease_expires_at")
    op.drop_column("jobs", "lease_owner")
    op.drop_column("jobs", "last_error")
    op.drop_column("jobs", "attempt")