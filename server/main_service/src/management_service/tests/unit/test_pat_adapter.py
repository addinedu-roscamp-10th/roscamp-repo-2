from __future__ import annotations

import json

from services.core.adapters.pat_adapter import PatAdapter


class _RecordingPatAdapter(PatAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.sent_orders: list[int] = []

    def start(self) -> None:
        self._started = True
        self._client = object()
        self._goal_cls = object()

    def _send_goal(self, order: int) -> tuple[bool, str]:
        self.sent_orders.append(order)
        return (True, f"sent:{order}")


def test_build_goal_accepts_strg_loc_without_strg_loc_id_for_place_action() -> None:
    """PAT adapter는 strg_loc payload만 받아도 적재 명령 order를 계산한다."""

    adapter = PatAdapter()

    order = adapter._build_goal(
        PatAdapter.PLACE_ACTION,
        {"item_id": 1001, "strg_loc": (2, 4)},
    )

    assert order == 124


def test_retrieve_action_sends_one_goal_per_batch_item() -> None:
    """출고 batch payload가 있으면 각 위치의 retrieve order를 순서대로 보낸다."""

    adapter = _RecordingPatAdapter()

    ok, message = adapter.execute(
        _item_id=1001,
        _robot_id="PAT",
        command=PatAdapter.RETRIEVE_ACTION,
        payload=json.dumps(
            {
                "item_id": 1001,
                "strg_loc": [2, 4],
                "batch": [
                    [1001, 2, 4],
                    [1002, 1, 2],
                    [1003, 3, 1],
                ],
            }
        ).encode("utf-8"),
    )

    assert ok is True
    assert message == "batch_retrieve_succeeded"
    assert adapter.sent_orders == [224, 212, 231]


def test_retrieve_action_rejects_invalid_batch_payload() -> None:
    """batch가 있으면 잘못된 위치를 strg_loc fallback으로 숨기지 않는다."""

    adapter = _RecordingPatAdapter()

    ok, message = adapter.execute(
        _item_id=1001,
        _robot_id="PAT",
        command=PatAdapter.RETRIEVE_ACTION,
        payload=json.dumps(
            {
                "item_id": 1001,
                "strg_loc": [2, 4],
                "batch": [[1001, 9, 4]],
            }
        ).encode("utf-8"),
    )

    assert ok is False
    assert message == "pat_retrieve_action_requires_valid_batch"
    assert adapter.sent_orders == []


def test_build_goal_accepts_strg_loc_without_strg_loc_id_for_retrieve_action() -> None:
    """PAT adapter는 strg_loc payload만 받아도 출고 명령 order를 계산한다."""

    adapter = PatAdapter()

    order = adapter._build_goal(
        PatAdapter.RETRIEVE_ACTION,
        {"item_id": 1001, "strg_loc": (2, 4)},
    )

    assert order == 224
