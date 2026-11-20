"""工作流 YAML：加载、轮数覆盖，以及加载器对非法定义的拒绝。"""

import pytest

from packages.application.travel import assurance_workflow, travel_workflow, workflow_source
from packages.core.engine.loader import WorkflowLoadError, load_workflow_text


def test_travel_workflow_is_loaded_from_yaml() -> None:
    workflow = travel_workflow(2)
    assert [node.id for node in workflow.nodes] == ["collect", "refine", "approval"]
    loop = workflow.nodes[1].loop
    assert loop is not None and loop.max_iterations == 2
    assert [node.id for node in loop.body] == ["plan", "assurance"]
    assert [node.id for node in assurance_workflow().nodes] == ["check", "review"]
    assert "type: loop" in workflow_source("travel")
    # 覆盖轮数不影响缓存中的原始定义
    assert travel_workflow(3).nodes[1].loop.max_iterations == 3  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            "id: w\nnodes:\n  - {id: a, type: agent, agent: x, depends_on: [b]}\n"
            "  - {id: b, type: agent, agent: x, depends_on: [a]}\n",
            "循环依赖",
        ),
        ("id: w\nnodes:\n  - {id: a, type: agent}\n", "必须提供 agent"),
        ("id: w\nnodes:\n  - {id: a, type: agent, agent: x, depends_on: [missing]}\n", "不存在"),
        (
            "id: w\nnodes:\n  - {id: a, type: loop,"
            " loop: {body: [{id: b, type: tool, tool: t}]}}\n",
            "max_iterations",
        ),
        (
            "id: w\nnodes:\n  - {id: a, type: tool, tool: t}\n  - {id: a, type: tool, tool: t}\n",
            "重复",
        ),
        ("!!python/object/apply:os.system ['echo hi']", "YAML"),
    ],
)
def test_loader_rejects_invalid_workflows(text: str, message: str) -> None:
    with pytest.raises(WorkflowLoadError, match=message):
        load_workflow_text(text)
