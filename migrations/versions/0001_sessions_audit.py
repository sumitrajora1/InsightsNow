"""Initial user-owned conversations, bounded requests and redacted audit records."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade():
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner", sa.String(512), nullable=False),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("state", sa.JSON(), nullable=True),
        sa.Column("active_request", sa.String(36), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_conversations_owner", "conversations", ["owner"])
    op.create_table(
        "query_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("owner", sa.String(512), nullable=False),
        sa.Column("grant_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for c in ("owner", "status", "session_id"):
        op.create_index(f"ix_query_requests_{c}", "query_requests", [c])
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("correlation_id", sa.String(36), nullable=False),
        sa.Column("owner", sa.String(512), nullable=False),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for c in ("request_id", "correlation_id", "owner"):
        op.create_index(f"ix_audit_events_{c}", "audit_events", [c])


def downgrade():
    raise RuntimeError("Destructive downgrade is intentionally disabled. Restore a reviewed database backup.")
