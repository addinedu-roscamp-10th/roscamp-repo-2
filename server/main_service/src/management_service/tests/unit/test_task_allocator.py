from __future__ import annotations

from management_service.services.contracts.enums import TaskType
from management_service.services.contracts.models import AmrRuntimeState
from management_service.services.core.task_allocator import TaskAllocator


class _DummyStateManager:
    """task_allocator._select_resource 단위 테스트용 stub.

    state_manager가 DB/seed에서 주입하는 master 데이터(transfer_points,
    battery_thresholds)를 테스트가 가정하는 값으로 직접 세팅한다.
    """

    transfer_points = {
        "ToCAST": (-0.256, 0.20),
        "ToINSP": (-0.67, -0.10),
        "ToPICK": (-0.223, -0.465),
    }
    battery_thresholds = {
        TaskType.ToPP: 30,
        TaskType.ToSTRG: 20,
        TaskType.ToSHIP: 25,
        TaskType.ToCHG: 15,
    }


def _allocator() -> TaskAllocator:
    # _select_resource 단위 테스트만 하므로 실제 state_manager 동작은 필요 없다.
    return TaskAllocator(state_manager=_DummyStateManager())


def test_select_resource_returns_none_when_no_amr_meets_battery_threshold() -> None:
    # 모든 후보가 최소 배터리 임계치보다 낮으면 할당하지 않아야 한다.
    allocator = _allocator()
    amr_stats = [
        AmrRuntimeState(res_id="TAT1", x=0.0, y=0.0, bat_pct=24),
        AmrRuntimeState(res_id="TAT2", x=1.0, y=0.0, bat_pct=20),
    ]

    selected = allocator._select_resource(
        available_resources=["TAT1", "TAT2"],
        task_type=TaskType.ToPP,
        amr_stats=amr_stats,
    )

    assert selected is None


def test_select_resource_returns_only_candidate_meeting_battery_threshold() -> None:
    # 여러 후보 중 임계치를 넘는 장비가 1대뿐이면 그 장비를 바로 선택해야 한다.
    allocator = _allocator()
    amr_stats = [
        AmrRuntimeState(res_id="TAT1", x=0.0, y=0.0, bat_pct=24),
        AmrRuntimeState(res_id="TAT2", x=1.0, y=0.0, bat_pct=40),
    ]

    selected = allocator._select_resource(
        available_resources=["TAT1", "TAT2"],
        task_type=TaskType.ToPP,
        amr_stats=amr_stats,
    )

    assert selected == "TAT2"


def test_select_resource_prefers_closer_amr_when_battery_scores_tie() -> None:
    # 배터리 조건이 같으면 출발 지점에 더 가까운 AMR이 우선되어야 한다.
    allocator = _allocator()
    amr_stats = [
        AmrRuntimeState(res_id="TAT1", x=-0.30, y=0.20, bat_pct=40),
        AmrRuntimeState(res_id="TAT2", x=2.00, y=0.20, bat_pct=40),
    ]

    selected = allocator._select_resource(
        available_resources=["TAT1", "TAT2"],
        task_type=TaskType.ToPP,
        amr_stats=amr_stats,
    )

    assert selected == "TAT1"


def test_select_resource_prefers_lower_remaining_battery_when_distances_tie() -> None:
    # 거리가 같으면 잔여 배터리가 더 적은 장비를 우선 선택해 배터리가 많은 장비를 보존해야 한다.
    allocator = _allocator()
    amr_stats = [
        AmrRuntimeState(res_id="TAT1", x=-0.256, y=0.20, bat_pct=30),
        AmrRuntimeState(res_id="TAT2", x=-0.256, y=0.20, bat_pct=80),
    ]

    selected = allocator._select_resource(
        available_resources=["TAT1", "TAT2"],
        task_type=TaskType.ToPP,
        amr_stats=amr_stats,
    )

    assert selected == "TAT1"


def test_select_resource_balances_distance_and_battery_scores() -> None:
    # 위치와 배터리 조건이 서로 상충하면 현재 가중치(거리 70, 배터리 30)로 최종 점수를 계산해 선택해야 한다.
    allocator = _allocator()
    amr_stats = [
        AmrRuntimeState(res_id="TAT1", x=-0.30, y=0.20, bat_pct=80),
        AmrRuntimeState(res_id="TAT2", x=-0.80, y=0.20, bat_pct=35),
    ]

    selected = allocator._select_resource(
        available_resources=["TAT1", "TAT2"],
        task_type=TaskType.ToPP,
        amr_stats=amr_stats,
    )

    # TAT1은 더 가깝지만 배터리가 많고, TAT2는 더 멀지만 잔여 배터리가 적다.
    # req_bat=30 이므로 remain_bat 은 TAT1=50, TAT2=5 이다.
    # 거리 정규화 결과는 TAT1 dist_score=1.0, TAT2 dist_score=0.0 이다.
    # 배터리 정규화 결과는 TAT1 bat_score=0.0, TAT2 bat_score=1.0 이다.
    # 최종 점수는 TAT1=(0.7*1.0)+(0.3*0.0)=0.7, TAT2=(0.7*0.0)+(0.3*1.0)=0.3 이다.
    # 따라서 현재 가중치에서는 TAT1이 선택되어야 한다.
    assert selected == "TAT1"


def test_select_resource_balances_distance_and_battery_scores_with_three_amrs() -> None:
    # 후보가 3대일 때도 거리/배터리 정규화와 가중합으로 가장 높은 점수를 고른다.
    allocator = _allocator()
    amr_stats = [
        AmrRuntimeState(res_id="TAT1", x=-0.30, y=0.20, bat_pct=42),
        AmrRuntimeState(res_id="TAT2", x=-0.55, y=0.20, bat_pct=60),
        AmrRuntimeState(res_id="TAT3", x=-0.80, y=0.20, bat_pct=35),
    ]

    selected = allocator._select_resource(
        available_resources=["TAT1", "TAT2", "TAT3"],
        task_type=TaskType.ToPP,
        amr_stats=amr_stats,
    )

    # req_bat=30 이므로 remain_bat 은 TAT1=12, TAT2=30, TAT3=5 이다.
    # dist_result = TAT1: 1.0, TAT2: 0.5, TAT3: 0.0
    # bat_result = TAT1: 0.72, TAT2: 0.0, TAT3: 1.0
    # result = TAT1: 0.916, TAT2: 0.35, TAT3: 0.3
    # best_res_id = TAT1
    assert selected == "TAT1"


def test_select_resource_returns_first_available_for_non_amr_resources() -> None:
    # AMR이 아닌 경우에 일반 리소스 할당 규칙대로 첫 번째 가용 리소스를 반환한다.
    allocator = _allocator()

    selected = allocator._select_resource(
        available_resources=["MAT1", "MAT2", "MAT3"],
        task_type=TaskType.MM,
        amr_stats=None,
    )

    assert selected == "MAT1"
