"""initial schema

The tables that `metadata.create_all` used to create at startup, now as a
tracked, reversible revision. Written to be portable: JSON becomes JSONB on
Postgres, booleans default via sa.false() (a literal '0' breaks on Postgres).

Revision ID: 0001
Revises:
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("model_config", JSON, nullable=False),
        sa.Column("allowed_tools", JSON, nullable=False),
        sa.Column("max_steps", sa.Integer(), nullable=False),
        sa.Column("max_cost_usd", sa.Float(), nullable=False),
        sa.Column("max_tokens", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_agents_tenant_id", "agents", ["tenant_id"])

    op.create_table(
        "user_permissions",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("permission", sa.String(100), primary_key=True),
    )

    op.create_table(
        "runs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("agent_id", sa.String(32), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("agent_version", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("input", sa.Text(), nullable=False),
        sa.Column("final_output", sa.Text()),
        sa.Column("messages", JSON, nullable=False),
        sa.Column("tainted", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("step_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completion_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("est_cost_usd", sa.Float(), server_default="0", nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("lease_owner", sa.String(64)),
        sa.Column("lease_expires_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_runs_tenant_status", "runs", ["tenant_id", "status", "updated_at"])

    op.create_table(
        "tool_calls",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(32), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(100), nullable=False),
        sa.Column("args", JSON, nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False, unique=True),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("decision_reason", sa.Text()),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("output", JSON),
        sa.Column("output_tainted", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("latency_ms", sa.Float()),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text()),
    )
    op.create_index("ix_tool_calls_tool_decision", "tool_calls", ["tool_name", "decision"])

    op.create_table(
        "approvals",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(32), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("tool_call_id", sa.String(64), nullable=False),
        sa.Column("tool_name", sa.String(100), nullable=False),
        sa.Column("args", JSON, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("edited_args", JSON),
        sa.Column("comment", sa.Text()),
        sa.Column("decided_by", sa.String(64)),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime()),
        sa.UniqueConstraint("run_id", "tool_call_id"),
    )
    op.create_index("ix_approvals_tenant_decision", "approvals", ["tenant_id", "decision"])

    op.create_table(
        "run_events",
        sa.Column("run_id", sa.String(32), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("data", JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "spans",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("parent_id", sa.String(32)),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("attributes", JSON, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime()),
        sa.Column("duration_ms", sa.Float()),
    )
    op.create_index("ix_spans_run_id", "spans", ["run_id"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target", sa.String(200), nullable=False),
        sa.Column("details", JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_audit_tenant_created", "audit_log", ["tenant_id", "created_at"])

    op.create_table(
        "memory_notes",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    # Reverse dependency order: children (FK holders) before parents.
    op.drop_table("memory_notes")
    op.drop_index("ix_audit_tenant_created", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_spans_run_id", table_name="spans")
    op.drop_table("spans")
    op.drop_table("run_events")
    op.drop_index("ix_approvals_tenant_decision", table_name="approvals")
    op.drop_table("approvals")
    op.drop_index("ix_tool_calls_tool_decision", table_name="tool_calls")
    op.drop_table("tool_calls")
    op.drop_index("ix_runs_tenant_status", table_name="runs")
    op.drop_table("runs")
    op.drop_table("user_permissions")
    op.drop_index("ix_agents_tenant_id", table_name="agents")
    op.drop_table("agents")
