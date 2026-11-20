"""审计与运行存储表；JSON 字段同时支持 PostgreSQL 与 SQLite 替身。"""

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

agents = Table(
    "agents",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("name", String(256), nullable=False),
)
agent_versions = Table(
    "agent_versions",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("agent_id", ForeignKey("agents.id"), nullable=False),
    Column("version", String(64), nullable=False),
    Column("definition", JSON, nullable=False),
    UniqueConstraint("agent_id", "version"),
)
workflows = Table(
    "workflows",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("version", String(64), primary_key=True),
    Column("definition", JSON, nullable=False),
)
runs = Table(
    "runs",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("workflow_id", String(128), nullable=False),
    Column("workflow_version", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("inputs", JSON, nullable=False),
    Column("seed", Integer, nullable=False),
    Column("mode", String(16), nullable=False),
    Column("budget_tokens", Integer),
    Column("spent_tokens", Integer, nullable=False, server_default="0"),
    CheckConstraint("mode IN ('live')", name="mode"),
    CheckConstraint(
        "status IN ('running','succeeded','failed','cancelled','awaiting_human')", name="status"
    ),
)
node_executions = Table(
    "node_executions",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("run_id", ForeignKey("runs.id"), nullable=False, index=True),
    Column("node_id", String(128), nullable=False),
    Column("iteration", Integer, nullable=False, server_default="0"),
    Column("candidate_index", Integer),
    Column("status", String(32), nullable=False),
    Column("agent_id", String(128)),
    Column("agent_version", String(64)),
    Column("prompt_variant", String(128)),
    Column("input_snapshot", JSON, nullable=False),
    Column("output_snapshot", JSON),
    Column("error", Text),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Column("tokens_in", Integer, nullable=False, server_default="0"),
    Column("tokens_out", Integer, nullable=False, server_default="0"),
    CheckConstraint(
        "status IN ('pending','ready','running','succeeded','failed','skipped','cancelled',"
        "'awaiting_human')",
        name="status",
    ),
)
llm_calls = Table(
    "llm_calls",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("run_id", ForeignKey("runs.id"), index=True),
    Column("node_execution_id", ForeignKey("node_executions.id")),
    Column("cache_key", String(64), nullable=False, index=True),
    Column("model", String(256), nullable=False),
    Column("mode", String(16), nullable=False),
    Column("request", JSON, nullable=False),
    Column("response", JSON),
    Column("tokens_in", Integer, nullable=False, server_default="0"),
    Column("tokens_out", Integer, nullable=False, server_default="0"),
    Column("duration", Float, nullable=False, server_default="0"),
    Column("cache_hit", Boolean, nullable=False, server_default="false"),
    Column("retries", Integer, nullable=False, server_default="0"),
    Column("error", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
