"""创建运行、Agent 版本、工作流、节点执行与模型调用审计表；表定义在本迁移内冻结。"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("name", sa.String(256), nullable=False),
    )
    op.create_table(
        "agent_versions",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("agent_id", sa.String(128), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("definition", sa.JSON, nullable=False),
        sa.UniqueConstraint("agent_id", "version"),
    )
    op.create_table(
        "workflows",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("version", sa.String(64), primary_key=True),
        sa.Column("definition", sa.JSON, nullable=False),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("workflow_id", sa.String(128), nullable=False),
        sa.Column("workflow_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("inputs", sa.JSON, nullable=False),
        sa.Column("seed", sa.Integer, nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("budget_tokens", sa.Integer),
        sa.Column("spent_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.CheckConstraint("mode IN ('live')", name="ck_runs_mode"),
        sa.CheckConstraint(
            "status IN ('running','succeeded','failed','cancelled','awaiting_human')",
            name="ck_runs_status",
        ),
    )
    op.create_table(
        "node_executions",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("run_id", sa.String(128), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("iteration", sa.Integer, nullable=False, server_default="0"),
        sa.Column("candidate_index", sa.Integer),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("agent_id", sa.String(128)),
        sa.Column("agent_version", sa.String(64)),
        sa.Column("prompt_variant", sa.String(128)),
        sa.Column("input_snapshot", sa.JSON, nullable=False),
        sa.Column("output_snapshot", sa.JSON),
        sa.Column("error", sa.Text),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("tokens_in", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer, nullable=False, server_default="0"),
        sa.CheckConstraint(
            "status IN ('pending','ready','running','succeeded','failed','skipped',"
            "'cancelled','awaiting_human')",
            name="ck_node_executions_status",
        ),
    )
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("run_id", sa.String(128), sa.ForeignKey("runs.id")),
        sa.Column("node_execution_id", sa.String(128), sa.ForeignKey("node_executions.id")),
        sa.Column("cache_key", sa.String(64), nullable=False),
        sa.Column("model", sa.String(256), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("request", sa.JSON, nullable=False),
        sa.Column("response", sa.JSON),
        sa.Column("tokens_in", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer, nullable=False, server_default="0"),
        sa.Column("duration", sa.Float, nullable=False, server_default="0"),
        sa.Column("cache_hit", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("retries", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for table in ("node_executions", "llm_calls"):
        op.create_index(f"ix_{table}_run_id", table, ["run_id"])
    op.create_index("ix_llm_calls_cache_key", "llm_calls", ["cache_key"])


def downgrade() -> None:
    for table in (
        "llm_calls",
        "node_executions",
        "runs",
        "workflows",
        "agent_versions",
        "agents",
    ):
        op.drop_table(table)
