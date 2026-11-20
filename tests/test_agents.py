"""YAML 角色卡 → 运行时定义，以及调度器的版本发现、技能匹配与负载约束。"""

from random import Random

import pytest

from packages.agents.loader import AgentLoadError, load_agent_catalog, load_agent_text
from packages.application.agents import AgentScheduler, default_agents, definition_from_spec


def scheduler() -> AgentScheduler:
    value = AgentScheduler()
    for agent in default_agents():
        value.agents[agent.key] = agent
    return value


def test_yaml_team_becomes_runtime_agents() -> None:
    agents = {agent.id: agent for agent in default_agents()}
    assert set(agents) == {
        "budget-planner",
        "nature-planner",
        "strict-reviewer",
        "experience-reviewer",
    }
    assert agents["nature-planner"].temperature == 0.4
    assert "knowledge" in agents["budget-planner"].tools
    assert "verify" in agents["budget-planner"].tools
    assert "catalog" not in agents["strict-reviewer"].tools


def test_yaml_with_unknown_role_is_rejected() -> None:
    spec = load_agent_text(
        "id: coder\nname: 修复工程师\nrole: code\ndescription: 不属于旅游领域的角色卡\n"
        "skills: [patch]\nprompt: 你是修复工程师，只修改与根因相关的代码。\n"
    )
    with pytest.raises(AgentLoadError):
        definition_from_spec(spec)


def test_yaml_loader_rejects_unsafe_documents() -> None:
    with pytest.raises(AgentLoadError):
        load_agent_text("!!python/object/apply:os.system ['echo hi']")
    with pytest.raises(AgentLoadError):
        load_agent_text("- just\n- a list\n")


def test_catalog_file_names_match_ids() -> None:
    specs = load_agent_catalog()
    assert len(specs) == 4 and all(spec.version == "1.2.0" for spec in specs)


def test_latest_version_wins() -> None:
    value = scheduler()
    newer = value.agents["budget-planner@1.2.0"].model_copy(update={"version": "1.3.0"})
    value.agents[newer.key] = newer
    keys = {agent.key for agent in value.candidates("planner")}
    assert "budget-planner@1.3.0" in keys and "budget-planner@1.2.0" not in keys


def test_reserve_respects_capacity_and_release() -> None:
    value = scheduler()
    rng = Random(1)
    first, trace = value.reserve("reviewer", {"budget"}, rng)
    second, _ = value.reserve("reviewer", {"budget"}, rng)
    assert first.key != second.key  # 每个审核员容量为 1
    assert {item["agent"] for item in trace} == {first.key, second.key}
    with pytest.raises(RuntimeError):
        value.reserve("reviewer", {"budget"}, rng)
    value.release(first.key)
    third, _ = value.reserve("reviewer", {"budget"}, rng)
    assert third.key == first.key


async def test_history_shifts_selection() -> None:
    """历史成功率高的规划师应被更频繁地选中（Thompson 采样）。"""
    value = scheduler()
    for _ in range(30):
        await value.profiles.update(
            "budget-planner@1.2.0", "planner", "travel", success=True, latency=1, cost=100
        )
        await value.profiles.update(
            "nature-planner@1.2.0", "planner", "travel", success=False, latency=1, cost=100
        )
    rng, wins = Random(7), 0
    for _ in range(50):
        agent, _ = value.reserve("planner", {"schedule"}, rng)
        wins += agent.id == "budget-planner"
        value.release(agent.key)
    assert wins >= 40
