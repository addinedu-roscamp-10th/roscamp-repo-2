"""
TATAdapter
==========
task_executor 가 호출하는 IAdapter 구현체 중 TAT(AMR) 전용.

- task_executor → adapter.send_command(res_id, action, params: dict) → AdapterResult
- 좌표 / goal pose 는 로봇 내부에 저장되어 있으므로 어댑터는 dock_id (= pose_name) 만 전달
- res_id 별 namespace 분리: e.g. /TAT1/dock_robot, /TAT2/dock_robot
- send_command 는 async 메서드. 호출 즉시 await 가능, 동시 디스패치 가능.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future as PyFuture
from dataclasses import dataclass
from typing import Callable, Dict, Type

import rclpy
from rclpy.action import ActionClient
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from action_msgs.msg import GoalStatus
from nav2_msgs.action import DockRobot

from services.contracts.models import AdapterResult


# ─── action_type 별 매핑 ─────────────────────────────────────────
@dataclass(frozen=True)
class ActionSpec:
    name: str                # 액션 이름 템플릿 ({res_id} 자리)
    action_type: Type        # 액션 메시지 클래스
    goal_builder: Callable   # (params: dict) → goal 만드는 함수


def _build_dock_goal(params: dict) -> DockRobot.Goal:
    """
    dock_id 방식 goal 생성. 좌표는 로봇 내부 dock DB에서 조회한다.

    필수 params:
        pose_name: 로봇 내부에 등록된 dock id
    """
    pose_name = params.get("pose_name")
    if not pose_name:
        raise ValueError("dock_robot requires params['pose_name']")

    goal = DockRobot.Goal()
    goal.use_dock_id = True
    goal.dock_id = pose_name
    goal.navigate_to_staging_pose = True
    return goal


# ─── 어댑터 ─────────────────────────────────────────────────────
class TATAdapter(Node):
    """task_executor → ROS2 action server (TAT 전용) 어댑터."""

    _ACTIONS: Dict[str, ActionSpec] = {
        "dock_robot": ActionSpec(
            name="/{res_id}/dock_robot",
            action_type=DockRobot,
            goal_builder=_build_dock_goal,
        ),
        # "undock_robot": ActionSpec(...)  # 추가 예정
    }

    def __init__(self, node_name: str = "tat_adapter") -> None:
        super().__init__(node_name)
        self._action_clients: Dict[str, ActionClient] = {}
        self._cb_group = ReentrantCallbackGroup()

        # 백그라운드 spin: 호출자 스레드를 점유하지 않고 ROS 콜백을 계속 처리
        self._ros_executor = MultiThreadedExecutor()
        self._ros_executor.add_node(self)
        self._spin_thread = threading.Thread(target=self._ros_executor.spin, daemon=True)
        self._spin_thread.start()

    # ── lifecycle ────────────────────────────────────────────
    def shutdown(self) -> None:
        """spin 스레드 정리 후 노드 파괴. 종료 시 반드시 호출."""
        self._ros_executor.shutdown()
        self._spin_thread.join(timeout=2.0)
        self.destroy_node()

    # ── public (IAdapter 구현) ───────────────────────────────
    async def send_command(self,
                           res_id: str,
                           action: str,
                           params: dict,
                           wait_server_sec: float = 5.0) -> AdapterResult:
        """task_executor 가 await 하는 비동기 진입점."""
        py_fut = self._dispatch(res_id, action, params, wait_server_sec)
        return await asyncio.wrap_future(py_fut)

    # ── private: 디스패치 + 콜백 체인 ─────────────────────────
    def _dispatch(self,
                  res_id: str,
                  action: str,
                  params: dict,
                  wait_server_sec: float) -> "PyFuture[AdapterResult]":
        py_future: PyFuture[AdapterResult] = PyFuture()

        # 1) 입력 검증
        if action not in self._ACTIONS:
            py_future.set_result(AdapterResult(
                success=False, message=f"unsupported action: {action}"
            ))
            return py_future

        spec = self._ACTIONS[action]
        try:
            goal_msg = spec.goal_builder(params)
        except ValueError as e:
            py_future.set_result(AdapterResult(success=False, message=str(e)))
            return py_future

        action_name = spec.name.format(res_id=res_id)

        # 2) ActionClient 준비
        client = self._get_client(action_name, spec.action_type)
        if not client.wait_for_server(timeout_sec=wait_server_sec):
            py_future.set_result(AdapterResult(
                success=False, message=f"action server unavailable: {action_name}"
            ))
            return py_future

        # 3) goal 송신 → 콜백 체인 (블록 없이 곧장 반환)
        send_goal_future = client.send_goal_async(goal_msg)
        send_goal_future.add_done_callback(
            lambda f: self._on_goal_response(f, action_name, res_id, action, py_future)
        )
        return py_future

    def _on_goal_response(self,
                          rclpy_fut,
                          action_name: str,
                          res_id: str,
                          action: str,
                          py_future: "PyFuture[AdapterResult]") -> None:
        try:
            goal_handle = rclpy_fut.result()
        except Exception as e:
            py_future.set_result(AdapterResult(
                success=False, message=f"send_goal exception: {e}"
            ))
            return

        if goal_handle is None or not goal_handle.accepted:
            py_future.set_result(AdapterResult(
                success=False, message=f"goal rejected by {action_name}"
            ))
            return

        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(
            lambda f: self._on_result(f, res_id, action, py_future)
        )

    def _on_result(self,
                   rclpy_fut,
                   res_id: str,
                   action: str,
                   py_future: "PyFuture[AdapterResult]") -> None:
        try:
            status = rclpy_fut.result().status
        except Exception as e:
            py_future.set_result(AdapterResult(
                success=False, message=f"get_result exception: {e}"
            ))
            return

        if status == GoalStatus.STATUS_SUCCEEDED:
            py_future.set_result(AdapterResult(
                success=True, message=f"{res_id} {action} succeeded"
            ))
        else:
            py_future.set_result(AdapterResult(
                success=False, message=f"{res_id} {action} not succeeded (status={status})"
            ))

    def _get_client(self, action_name: str, action_type: Type) -> ActionClient:
        if action_name not in self._action_clients:
            self._action_clients[action_name] = ActionClient(
                self, action_type, action_name,
                callback_group=self._cb_group,
            )
        return self._action_clients[action_name]