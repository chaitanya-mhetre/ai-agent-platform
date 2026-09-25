"""Tables (SQLAlchemy Core). Portable across PostgreSQL (prod) and SQLite (tests).

All timestamps are naive UTC (see store.sql.utcnow), so SQLite and Postgres
compare them the same way.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

metadata = sa.MetaData()
JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

agents = sa.Table(
    "agents",
    metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("system_prompt", sa.Text, nullable=False),
    sa.Column("model_config", JSON, nullable=False),
    sa.Column("allowed_tools", JSON, nullable=False),
    sa.Column("max_steps", sa.Integer, nullable=False),
    sa.Column("max_cost_usd", sa.Float, nullable=False),
    sa.Column("max_tokens", sa.Integer, nullable=False),
    sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    sa.Column("created_at", sa.DateTime(), nullable=False),
)

user_permissions = sa.Table(
    "user_permissions",
    metadata,
    sa.Column("tenant_id", sa.String(64), primary_key=True),
    sa.Column("user_id", sa.String(64), primary_key=True),
    sa.Column("permission", sa.String(100), primary_key=True),
)

runs = sa.Table(
    "runs",
    metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("agent_id", sa.String(32), sa.ForeignKey("agents.id"), nullable=False),
    sa.Column("agent_version", sa.Integer, nullable=False),
    sa.Column("user_id", sa.String(64), nullable=False),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("input", sa.Text, nullable=False),
    sa.Column("final_output", sa.Text),
    sa.Column("messages", JSON, nullable=False),
    sa.Column("tainted", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("step_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("prompt_tokens", sa.Integer, nullable=False, server_default="0"),
    sa.Column("completion_tokens", sa.Integer, nullable=False, server_default="0"),
    sa.Column("est_cost_usd", sa.Float, nullable=False, server_default="0"),
    sa.Column("cancel_requested", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("error", sa.Text),
    sa.Column("lease_owner", sa.String(64)),
    sa.Column("lease_expires_at", sa.DateTime()),
    sa.Column("created_at", sa.DateTime(), nullable=False),
    sa.Column("updated_at", sa.DateTime(), nullable=False),
    sa.Index("ix_runs_tenant_status", "tenant_id", "status", "updated_at"),
)

tool_calls = sa.Table(
    "tool_calls",
    metadata,
    sa.Column("id", sa.String(64), primary_key=True),  # the model's call id, scoped by run
    sa.Column("run_id", sa.String(32), sa.ForeignKey("runs.id"), primary_key=True),
    sa.Column("step", sa.Integer, nullable=False),
    sa.Column("tool_name", sa.String(100), nullable=False),
    sa.Column("args", JSON, nullable=False),
    sa.Column("idempotency_key", sa.String(64), nullable=False, unique=True),
    sa.Column("decision", sa.String(32), nullable=False),
    sa.Column("decision_reason", sa.Text),
    sa.Column("status", sa.String(32), nullable=False),  # started|succeeded|failed|skipped
    sa.Column("output", JSON),
    sa.Column("output_tainted", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("latency_ms", sa.Float),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("error", sa.Text),
    sa.Index("ix_tool_calls_tool_decision", "tool_name", "decision"),
)

approvals = sa.Table(
    "approvals",
    metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("run_id", sa.String(32), sa.ForeignKey("runs.id"), nullable=False),
    sa.Column("tool_call_id", sa.String(64), nullable=False),
    sa.Column("tool_name", sa.String(100), nullable=False),
    sa.Column("args", JSON, nullable=False),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("decision", sa.String(16), nullable=False),  # pending|approved|rejected|edited
    sa.Column("edited_args", JSON),
    sa.Column("comment", sa.Text),
    sa.Column("decided_by", sa.String(64)),
    sa.Column("requested_at", sa.DateTime(), nullable=False),
    sa.Column("decided_at", sa.DateTime()),
    sa.UniqueConstraint("run_id", "tool_call_id"),
    sa.Index("ix_approvals_tenant_decision", "tenant_id", "decision"),
)

run_events = sa.Table(
    "run_events",
    metadata,
    sa.Column("run_id", sa.String(32), sa.ForeignKey("runs.id"), primary_key=True),
    sa.Column("seq", sa.Integer, primary_key=True),
    sa.Column("type", sa.String(40), nullable=False),
    sa.Column("data", JSON, nullable=False),
    sa.Column("created_at", sa.DateTime(), nullable=False),
)

spans = sa.Table(
    "spans",
    metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("run_id", sa.String(32), nullable=False, index=True),
    sa.Column("parent_id", sa.String(32)),
    sa.Column("kind", sa.String(20), nullable=False),  # run|model_call|tool_call
    sa.Column("name", sa.String(100), nullable=False),
    sa.Column("attributes", JSON, nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("started_at", sa.DateTime(), nullable=False),
    sa.Column("ended_at", sa.DateTime()),
    sa.Column("duration_ms", sa.Float),
)

audit_log = sa.Table(
    "audit_log",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("actor", sa.String(16), nullable=False),  # user|agent|system
    sa.Column("actor_id", sa.String(64), nullable=False),
    sa.Column("action", sa.String(64), nullable=False),
    sa.Column("target", sa.String(200), nullable=False),
    sa.Column("details", JSON, nullable=False),
    sa.Column("created_at", sa.DateTime(), nullable=False),
    sa.Index("ix_audit_tenant_created", "tenant_id", "created_at"),
)

memory_notes = sa.Table(
    "memory_notes",
    metadata,
    sa.Column("tenant_id", sa.String(64), primary_key=True),
    sa.Column("user_id", sa.String(64), primary_key=True),
    sa.Column("key", sa.String(100), primary_key=True),
    sa.Column("value", sa.Text, nullable=False),
    sa.Column("updated_at", sa.DateTime(), nullable=False),
)
