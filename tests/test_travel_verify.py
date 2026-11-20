"""确定性行程校验：费用、时间窗、交通间隔、兴趣与必去景点。"""

from typing import Any

import pytest
from pydantic import ValidationError

from packages.application.travel import Itinerary, TravelRequest, solve, verify_plan
from tests.helpers import BAD_PLAN, GOOD_PLAN


def check(plan: dict[str, Any], **request: Any) -> dict[str, Any]:
    return verify_plan(Itinerary.model_validate(plan), TravelRequest(**request))


def test_good_plan_passes_with_exact_cost() -> None:
    result = check(GOOD_PLAN)
    assert result["passed"], result["errors"]
    # 餐饮 40 + 门票 0+40+30 + 两段交通 20
    assert result["total_cost"] == 130


def test_bad_plan_reports_every_violation() -> None:
    result = check(BAD_PLAN)
    assert not result["passed"]
    text = "；".join(result["errors"])
    assert "tower 开始时间早于日程或开门时间" in text
    assert "超过预算" in text


def test_budget_is_enforced() -> None:
    result = check(GOOD_PLAN, budget=120)
    assert not result["passed"]
    assert any("超过预算 120" in error for error in result["errors"])


def test_transfer_gap_of_20_minutes_is_required() -> None:
    plan = {**GOOD_PLAN, "stops": [dict(stop) for stop in GOOD_PLAN["stops"]]}
    plan["stops"][1]["start_minute"] = 640  # 公园 10:30 结束，10:40 开始只留了 10 分钟
    assert any("20 分钟交通" in error for error in check(plan)["errors"])


def test_closing_time_is_enforced() -> None:
    plan = {
        "stops": [
            {"spot_id": "park", "start_minute": 540},
            {"spot_id": "museum", "start_minute": 910},  # 15:10 开始，17:10 结束晚于闭馆
        ],
        "rationale": "x",
    }
    assert any("超出日程或营业时间" in e for e in check(plan, min_stops=2)["errors"])


def test_interests_must_visit_and_unknown_spot() -> None:
    plan = {
        "stops": [
            {"spot_id": "park", "start_minute": 540},
            {"spot_id": "nowhere", "start_minute": 700},
        ],
        "rationale": "x",
    }
    errors = check(plan, min_stops=1, must_visit=["museum"])["errors"]
    assert "景点 nowhere 不在目录" in errors
    assert "未覆盖全部必去景点" in errors
    assert "未覆盖全部兴趣类别" in errors


def test_duplicate_spot_is_rejected() -> None:
    plan = {
        "stops": [
            {"spot_id": "park", "start_minute": 540},
            {"spot_id": "park", "start_minute": 660},
        ],
        "rationale": "x",
    }
    assert "景点 park 重复" in check(plan, min_stops=1, interests=["nature"])["errors"]


def test_request_validation() -> None:
    with pytest.raises(ValidationError):
        TravelRequest(day_start=600, day_end=600)
    with pytest.raises(ValidationError):
        TravelRequest(must_visit=["unknown"])
    with pytest.raises(ValidationError):
        TravelRequest(weekday=8)
    with pytest.raises(ValidationError):
        Itinerary.model_validate({**GOOD_PLAN, "total_cost": 0})  # 不接受模型自报字段


def test_solver_finds_cheapest_feasible_plan() -> None:
    best = solve(TravelRequest())
    assert best is not None
    plan, cost = best
    assert cost == 60  # 公园 + 老街 + 湖畔步道，全部免费
    assert [stop.spot_id for stop in plan.stops] == ["park", "old_street", "lake"]
    assert verify_plan(plan, TravelRequest())["passed"]


def test_solver_reports_infeasible_constraints() -> None:
    assert solve(TravelRequest(budget=85, min_stops=4)) is None  # 4 景点最低 90 元
    assert solve(TravelRequest(budget=170, must_visit=["tower"])) is None  # 含观景塔最低 180 元
    assert solve(TravelRequest(weekday=1, must_visit=["museum"])) is None  # 周一闭馆
    assert solve(TravelRequest(weather="rainy", budget=100)) is None  # 雨天最低 150 元


def test_solver_respects_weekday_and_weather() -> None:
    best = solve(TravelRequest(weather="rainy", weekday=1, budget=300))
    assert best is not None
    request = TravelRequest(weather="rainy", weekday=1, budget=300)
    assert verify_plan(best[0], request)["passed"]
    assert not {"museum", "science"} & {stop.spot_id for stop in best[0].stops}


def test_closed_day_is_enforced() -> None:
    errors = check(GOOD_PLAN, weekday=1)["errors"]
    assert "museum 周一闭馆" in errors


def test_rainy_day_limits_outdoor_spots() -> None:
    errors = check(GOOD_PLAN, weather="rainy")["errors"]  # 公园、植物园都在户外
    assert "雨天最多安排 1 个户外景点，当前 2 个" in errors
