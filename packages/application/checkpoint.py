"""业务节点审计增强：保留真实 Agent 版本与开始时间，关联模型调用。"""

from packages.core.engine.checkpoint import SqlCheckpointStore
from packages.core.models.run import NodeExecution


class RuntimeCheckpointStore(SqlCheckpointStore):
    async def begin(self, execution: NodeExecution) -> None:
        if not hasattr(self, "active"):
            self.active: dict[tuple[str, str, int, int | None], NodeExecution] = {}
        self.active[self._key(execution)] = execution
        await super().record(execution)

    @staticmethod
    def _key(execution: NodeExecution) -> tuple[str, str, int, int | None]:
        return (execution.run_id, execution.node_id, execution.iteration, execution.candidate_index)

    async def record(self, execution: NodeExecution) -> None:
        active = getattr(self, "active", {})
        initial = active.pop(self._key(execution), None)
        if initial is not None:
            execution = execution.model_copy(
                update={
                    "id": initial.id,
                    "agent_id": initial.agent_id,
                    "agent_version": initial.agent_version,
                    "prompt_variant": initial.prompt_variant,
                    "started_at": initial.started_at,
                }
            )
        await super().record(execution)
