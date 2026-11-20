"""受控旅游计划示例：固定目录、严格结构和独立确定性校验，不代表实时报价。"""

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.core.engine.loader import load_workflow
from packages.core.models.workflow import WorkflowDef

CATALOG: list[dict[str, Any]] = [
    # indoor：是否室内（雨天户外景点最多 1 个）；closed_on：每周闭馆日，1=周一 … 7=周日。
    {
        "id": "museum",
        "name": "示例城市博物馆",
        "price": 40,
        "minutes": 120,
        "opens": 540,
        "closes": 1020,
        "tags": ["culture"],
        "indoor": True,
        "closed_on": [1],
    },
    {
        "id": "park",
        "name": "示例滨河公园",
        "price": 0,
        "minutes": 90,
        "opens": 480,
        "closes": 1140,
        "tags": ["nature"],
        "indoor": False,
        "closed_on": [],
    },
    {
        "id": "garden",
        "name": "示例植物园",
        "price": 30,
        "minutes": 120,
        "opens": 540,
        "closes": 1020,
        "tags": ["nature"],
        "indoor": False,
        "closed_on": [],
    },
    {
        "id": "gallery",
        "name": "示例艺术馆",
        "price": 50,
        "minutes": 90,
        "opens": 600,
        "closes": 1080,
        "tags": ["culture"],
        "indoor": True,
        "closed_on": [2],
    },
    {
        "id": "tower",
        "name": "示例观景塔",
        "price": 120,
        "minutes": 60,
        "opens": 600,
        "closes": 1080,
        "tags": ["culture", "nature"],
        "indoor": False,
        "closed_on": [],
    },
    {
        "id": "old_street",
        "name": "示例老街",
        "price": 0,
        "minutes": 60,
        "opens": 480,
        "closes": 1320,
        "tags": ["culture"],
        "indoor": False,
        "closed_on": [],
    },
    {
        "id": "temple",
        "name": "示例古寺",
        "price": 20,
        "minutes": 60,
        "opens": 480,
        "closes": 1050,
        "tags": ["culture"],
        "indoor": False,
        "closed_on": [],
    },
    {
        "id": "lake",
        "name": "示例湖畔步道",
        "price": 0,
        "minutes": 75,
        "opens": 360,
        "closes": 1260,
        "tags": ["nature"],
        "indoor": False,
        "closed_on": [],
    },
    {
        "id": "aquarium",
        "name": "示例海洋馆",
        "price": 90,
        "minutes": 120,
        "opens": 540,
        "closes": 1080,
        "tags": ["nature"],
        "indoor": True,
        "closed_on": [3],
    },
    {
        "id": "science",
        "name": "示例科技馆",
        "price": 60,
        "minutes": 120,
        "opens": 570,
        "closes": 1050,
        "tags": ["culture"],
        "indoor": True,
        "closed_on": [1],
    },
]
MEAL_COST = 40
WEEKDAYS = "一二三四五六日"
MAX_STOPS = 5
# 每个 Agent 步骤最多与模型往返的次数（含工具调用轮次与最终回答）。
MAX_AGENT_TURNS = 3
# 每轮迭代 = 一次规划 + 一次审核。
CALLS_PER_ITERATION = 2 * MAX_AGENT_TURNS
RUN_TOKEN_BUDGET = 40000
TRANSFER_COST = 10
TRANSFER_MINUTES = 20
SOURCE_NOTE = "人工编制的固定测试数据，不是实际景点、营业信息或实时价格。金额单位：元。"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _interests() -> list[Literal["culture", "nature"]]:
    return ["culture", "nature"]


class TravelRequest(StrictModel):
    topic: str = Field(default="安排一日文化与自然之旅", min_length=1, max_length=500)
    budget: int = Field(default=200, ge=40, le=2000)
    day_start: int = Field(default=540, ge=360, le=1200)
    day_end: int = Field(default=1080, ge=420, le=1320)
    min_stops: int = Field(default=3, ge=1, le=5)
    interests: list[Literal["culture", "nature"]] = Field(
        default_factory=_interests, min_length=1, max_length=2
    )
    must_visit: list[str] = Field(default_factory=list, max_length=5)
    weekday: int = Field(default=6, ge=1, le=7, description="1=周一 … 7=周日")
    weather: Literal["sunny", "rainy"] = "sunny"
    max_iterations: int = Field(default=3, ge=1, le=3)
    priority: int = Field(default=5, ge=0, le=10)
    human_review: bool = Field(default=True, strict=True)
    confirm_paid: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def check_constraints(self) -> "TravelRequest":
        if self.day_end <= self.day_start:
            raise ValueError("结束时间必须晚于开始时间")
        known = {item["id"] for item in CATALOG}
        if not set(self.must_visit) <= known or len(set(self.must_visit)) != len(self.must_visit):
            raise ValueError("必去景点必须为目录内不重复的 ID")
        return self


class Stop(StrictModel):
    spot_id: str = Field(min_length=1, max_length=40)
    start_minute: int = Field(strict=True, ge=0, le=1440)


class Itinerary(StrictModel):
    stops: list[Stop] = Field(min_length=1, max_length=MAX_STOPS)
    rationale: str = Field(min_length=1, max_length=1200)


class Review(StrictModel):
    approved: bool = Field(strict=True)
    feedback: list[str] = Field(max_length=8)


def verify_plan(plan: Itinerary, request: TravelRequest) -> dict[str, Any]:
    """金额和时长从工具目录计算，不信任模型自行声称的费用或通过标记。"""
    errors: list[str] = []
    catalog = {str(item["id"]): item for item in CATALOG}
    seen: set[str] = set()
    tags: set[str] = set()
    cost = MEAL_COST  # 固定餐饮预留，与 constraints 工具返回保持一致。
    previous_end = request.day_start
    outdoor = 0
    rows: list[dict[str, Any]] = []
    for index, stop in enumerate(plan.stops):
        if stop.spot_id not in catalog:
            errors.append(f"景点 {stop.spot_id} 不在目录")
            continue
        item = catalog[stop.spot_id]
        if stop.spot_id in seen:
            errors.append(f"景点 {stop.spot_id} 重复")
        seen.add(stop.spot_id)
        tags.update(item["tags"])
        transfer = TRANSFER_MINUTES if index else 0
        cost += int(item["price"]) + (TRANSFER_COST if index else 0)
        end = stop.start_minute + int(item["minutes"])
        if stop.start_minute < previous_end + transfer:
            errors.append(f"{stop.spot_id} 与上个活动冲突或未预留 20 分钟交通")
        if stop.start_minute < max(request.day_start, int(item["opens"])):
            errors.append(f"{stop.spot_id} 开始时间早于日程或开门时间")
        if end > min(request.day_end, int(item["closes"])):
            errors.append(f"{stop.spot_id} 结束时间超出日程或营业时间")
        if request.weekday in item["closed_on"]:
            errors.append(f"{stop.spot_id} 周{WEEKDAYS[request.weekday - 1]}闭馆")
        outdoor += not item["indoor"]
        previous_end = end
        rows.append({**item, "start_minute": stop.start_minute, "end_minute": end})
    if cost > request.budget:
        errors.append(f"实际费用 {cost} 元，超过预算 {request.budget} 元")
    if len(seen) < request.min_stops:
        errors.append(f"不同景点数不足 {request.min_stops}")
    if not set(request.must_visit) <= seen:
        errors.append("未覆盖全部必去景点")
    if not set(request.interests) <= tags:
        errors.append("未覆盖全部兴趣类别")
    if request.weather == "rainy" and outdoor > 1:
        errors.append(f"雨天最多安排 1 个户外景点，当前 {outdoor} 个")
    return {
        "passed": not errors,
        "errors": errors,
        "total_cost": cost,
        "budget": request.budget,
        "stops": rows,
        "source_note": SOURCE_NOTE,
    }


WORKFLOW_DIR = Path(__file__).resolve().parent / "workflows"


@lru_cache(maxsize=4)
def _load(name: str) -> WorkflowDef:
    return load_workflow(WORKFLOW_DIR / f"{name}.yaml")


def workflow_source(name: str) -> str:
    """工作流 YAML 原文，供页面展示。"""
    return (WORKFLOW_DIR / f"{name}.yaml").read_text(encoding="utf-8")


def travel_workflow(iterations: int = 3) -> WorkflowDef:
    """从 workflows/travel.yaml 加载，并用本次运行的迭代上限覆盖循环轮数。"""
    workflow = _load("travel").model_copy(deep=True)
    for node in workflow.nodes:
        if node.type == "loop" and node.loop is not None:
            node.loop.max_iterations = iterations
            node.loop.budget_tokens = RUN_TOKEN_BUDGET
    return workflow


def assurance_workflow() -> WorkflowDef:
    return _load("assurance").model_copy(deep=True)


def schedule(order: tuple[str, ...], request: TravelRequest) -> Itinerary:
    """按给定顺序把每个景点排在最早可行的开始时间。"""
    catalog = {str(item["id"]): item for item in CATALOG}
    stops, previous_end = [], request.day_start
    for index, spot_id in enumerate(order):
        item = catalog[spot_id]
        start = max(previous_end + (TRANSFER_MINUTES if index else 0), int(item["opens"]))
        stops.append(Stop(spot_id=spot_id, start_minute=start))
        previous_end = start + int(item["minutes"])
    return Itinerary(stops=stops, rationale="穷举基线：最低费用的可行顺序")


def solve(request: TravelRequest) -> tuple[Itinerary, int] | None:
    """剪枝深度优先搜索全部景点顺序，返回费用最低的可行行程；无解返回 None。

    每个顺序按最早可行时间排程（对给定顺序最优）；费用、营业时间、闭馆日、雨天户外上限
    任一不满足即剪枝。只用作评估基线：判断约束是否有解、衡量模型方案与最低费用的差距。
    """
    catalog = {str(item["id"]): item for item in CATALOG}
    usable = [
        spot_id for spot_id, item in catalog.items() if request.weekday not in item["closed_on"]
    ]
    best: list[Any] = [None, request.budget + 1]

    def search(order: list[str], end: int, cost: int, tags: set[str], outdoor: int) -> None:
        if (
            len(order) >= request.min_stops
            and set(request.must_visit) <= set(order)
            and set(request.interests) <= tags
            and cost < best[1]
        ):
            best[0], best[1] = list(order), cost
        if len(order) == MAX_STOPS:
            return
        for spot_id in usable:
            if spot_id in order:
                continue
            item = catalog[spot_id]
            extra = int(item["price"]) + (TRANSFER_COST if order else 0)
            more_outdoor = outdoor + (not item["indoor"])
            if cost + extra >= best[1] or (request.weather == "rainy" and more_outdoor > 1):
                continue
            begin = max(end + (TRANSFER_MINUTES if order else 0), int(item["opens"]))
            finish = begin + int(item["minutes"])
            if finish > min(request.day_end, int(item["closes"])):
                continue
            order.append(spot_id)
            search(order, finish, cost + extra, tags | set(item["tags"]), more_outdoor)
            order.pop()

    search([], request.day_start, MEAL_COST, set(), 0)
    if best[0] is None:
        return None
    plan = schedule(tuple(best[0]), request)
    assert verify_plan(plan, request)["passed"], "求解器结果必须通过确定性校验"
    return plan, int(best[1])
