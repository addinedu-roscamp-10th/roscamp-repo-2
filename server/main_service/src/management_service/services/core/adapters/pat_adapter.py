from __future__ import annotations

from typing import Any, TYPE_CHECKING

from services.contracts.models import AdapterResult
from services.core.adapters.ros2_adapter_base import BaseRos2Adapter, _DomainSession

if TYPE_CHECKING:
    from services.core.adapters.ros2_runtime import Ros2RuntimePool


class PatAdapter(BaseRos2Adapter):
    """Pat(적재&출고) Ros2 action client."""

    _ACTION_NAME = "execute_task"
    _STRG_COLUMNS = 6
    _STRG_SLOT_COUNT = 18

    PICK_ACTION = "pat_pick_action"
    PLACE_ACTION = "pat_place_storage_action"
    RETRIEVE_ACTION = "pat_retrieve_action"
    DEFECT_ACTION = "pat_defect_drop_action"

    _ACTIONS = {PICK_ACTION, PLACE_ACTION, RETRIEVE_ACTION, DEFECT_ACTION}

    def __init__(self, runtime_pool: Ros2RuntimePool | None = None) -> None:
        super().__init__(runtime_pool=runtime_pool)
        self._action_type: Any | None = None

    def start(self) -> None:
        if self._started:
            return
        if self._runtime_pool is None:
            return

        try:
            from example_interfaces.action import Fibonacci
            from rclpy.action import ActionClient
            from rclpy.node import Node
        except ImportError:
            return

        self._node_cls = Node
        self._action_client_cls = ActionClient
        self._action_type = Fibonacci
        self._started = True

    async def send_command(
        self,
        res_id: str,
        action: str,
        params: dict[str, Any],
    ) -> AdapterResult:
        if action not in self._ACTIONS:
            return AdapterResult(success=False, message=f"unsupported_pat_command:{action}")
        if not res_id:
            return AdapterResult(success=False, message="pat_robot_id_required")

        wait_sec = float(params.get("wait_server_sec", 5.0))
        timeout_sec = float(params.get("result_timeout_sec", 300.0))

        if action == self.RETRIEVE_ACTION and params.get("batch"):
            batch = self._storage_batch(params.get("batch"))
            if batch is None:
                return AdapterResult(success=False, message=f"{action}_requires_valid_batch")

            orders = [200 + row * 10 + col for _, row, col in batch]
            return await self._send_goals(
                res_id, orders,
                wait_sec=wait_sec,
                timeout_sec=timeout_sec,
                success_message="batch_retrieve_succeeded",
            )

        order = self._build_goal(action, params)
        if order is None:
            return AdapterResult(success=False, message=f"{action}_requires_strg_loc")

        return await self._send_goals(res_id, [order], wait_sec=wait_sec, timeout_sec=timeout_sec)

    async def _send_goals(
        self,
        res_id: str,
        orders: list[int],
        *,
        wait_sec: float,
        timeout_sec: float,
        success_message: str | None = None,
    ) -> AdapterResult:
        self.start()
        if not self._started:
            return AdapterResult(success=False, message="pat_adapter_unavailable")

        session = self._session_for(res_id)
        if session is None:
            return AdapterResult(success=False, message="pat_adapter_unavailable")

        for order in orders:
            ok, message = await self._send_pat_goal(session, order, wait_sec, timeout_sec)
            if not ok:
                return AdapterResult(success=False, message=message)
        return AdapterResult(
            success=True,
            message=success_message or f"pat_order_{orders[-1]}_succeeded",
        )

    async def _send_pat_goal(
        self,
        session: _DomainSession,
        order: int,
        wait_sec: float,
        timeout_sec: float,
    ) -> tuple[bool, str]:
        # 호출자(_send_goals)가 _started 를 보장하므로 _action_type 는 non-None.
        action_type = self._action_type
        client = self._get_or_create_client(session, self._ACTION_NAME, action_type)

        goal = action_type.Goal()
        goal.order = order

        def parse_result(wrapped: Any) -> tuple[bool, str]:
            sequence = list(getattr(wrapped.result, "sequence", []))
            success = bool(sequence and int(sequence[0]) == 1)
            return (
                success,
                f"pat_order_{order}_{'succeeded' if success else 'failed'}",
            )

        return await self._send_single_goal_async(
            client,
            goal,
            parse_result,
            prefix="pat",
            action_name=self._ACTION_NAME,
            rejected_tag=str(order),
            timeout_tag=str(order),
            wait_sec=wait_sec,
            timeout_sec=timeout_sec,
        )

    def _build_goal(self, action: str, params: dict[str, Any]) -> int | None:
        if action == self.PICK_ACTION:
            return 400
        if action == self.DEFECT_ACTION:
            return 300

        location = self._storage_location(params.get("strg_loc"))
        if location is None:
            return None
        row, col = location
        if action == self.PLACE_ACTION:
            return 100 + row * 10 + col
        if action == self.RETRIEVE_ACTION:
            return 200 + row * 10 + col
        return None

    @classmethod
    def _storage_location(cls, raw: Any | None) -> tuple[int, int] | None:
        if raw is None:
            return None
        if isinstance(raw, (tuple, list)) and len(raw) == 2:
            try:
                row = int(raw[0])
                col = int(raw[1])
            except (TypeError, ValueError):
                return None
            if row < 1 or row > (cls._STRG_SLOT_COUNT // cls._STRG_COLUMNS):
                return None
            if col < 1 or col > cls._STRG_COLUMNS:
                return None
            return (row, col)
        return None

    @classmethod
    def _storage_batch(cls, raw: Any | None) -> list[tuple[int, int, int]] | None:
        if raw is None or not isinstance(raw, (tuple, list)) or len(raw) == 0:
            return None

        batch: list[tuple[int, int, int]] = []
        for entry in raw:
            if not isinstance(entry, (tuple, list)) or len(entry) != 3:
                return None
            try:
                item_id = int(entry[0])
            except (TypeError, ValueError):
                return None

            location = cls._storage_location((entry[1], entry[2]))
            if location is None:
                return None
            row, col = location
            batch.append((item_id, row, col))

        return batch
