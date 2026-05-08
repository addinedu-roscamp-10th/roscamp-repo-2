"""
TATAdapter
==========
상위 모듈의 (robot_id, pose_name, action_type) 명령을
실제 좌표 + ROS2 action goal로 매핑하여 전송하는 어댑터.

- 외부 pose 테이블(YAML)에서 `pose_name → (x, y, theta)` 변환
- robot_id 별 namespace 분리: e.g. /TAT1/navigate_to_pose, /TAT2/dock_robot
- action_type 별 ActionSpec 등록으로 (액션 이름·메시지 타입·goal 빌더)를 묶어 관리
- send_command 는 호출 즉시 PyFuture 를 반환하므로
  여러 로봇에 동시 명령 디스패치가 가능하다.
"""

from __future__ import annotations

import math
import threading
from concurrent.futures import Future as PyFuture
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Tuple, Type

import yaml
import rclpy
from rclpy.action import ActionClient
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped
from nav2_msgs.action import NavigateToPose, DockRobot
from pydantic import BaseModel


# ─── 결과 모델 ──────────────────────────────────────────────────
class SendCommandResult(BaseModel):
    """send_command() 단일 동작 실행 결과."""
    succeeded: bool
    message: str


# ─── pose 로딩 / 변환 ────────────────────────────────────────────
@dataclass(frozen=True)
class Pose2D:
    """2D pose. x, y in meter / theta in radian."""
    x: float
    y: float
    theta: float


def load_pose_table(path: str | Path) -> Dict[str, Pose2D]:
    """YAML 파일에서 pose 테이블을 로드한다."""
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    table: Dict[str, Pose2D] = {}
    for name, val in raw.items():
        try:
            table[name] = Pose2D(x=float(val["x"]), y=float(val["y"]), theta=float(val["theta"]))
        except (KeyError, TypeError) as e:
            raise ValueError(f"invalid pose entry for '{name}': {val}") from e
    return table


def _yaw_to_quaternion(yaw: float) -> Tuple[float, float, float, float]:
    """평면 yaw(rad) → quaternion(x, y, z, w)."""
    half = yaw * 0.5
    return (0.0, 0.0, math.sin(half), math.cos(half))


def _make_pose_stamped(node: Node, pose: Pose2D, frame_id: str = "map") -> PoseStamped:
    qx, qy, qz, qw = _yaw_to_quaternion(pose.theta)
    ps = PoseStamped()
    ps.header.frame_id = frame_id
    ps.header.stamp = node.get_clock().now().to_msg()
    ps.pose.position.x = pose.x
    ps.pose.position.y = pose.y
    ps.pose.position.z = 0.0
    ps.pose.orientation.x = qx
    ps.pose.orientation.y = qy
    ps.pose.orientation.z = qz
    ps.pose.orientation.w = qw
    return ps


# ─── action_type 별 매핑 ─────────────────────────────────────────
@dataclass(frozen=True)
class ActionSpec:
    name: str                # 액션 이름 템플릿 ({robot_id} 자리)
    action_type: Type        # 메시지 클래스
    goal_builder: Callable   # goal 만드는 함수


# 얘는 보내는 방식이 dock pose랑 달라서 수정해야함
def _build_navigate_goal(node: Node, pose: Pose2D) -> NavigateToPose.Goal:
    goal = NavigateToPose.Goal()
    goal.pose = _make_pose_stamped(node, pose)
    return goal


def _build_dock_goal(node: Node, pose: Pose2D) -> DockRobot.Goal:
    goal = DockRobot.Goal()
    goal.use_dock_id = False
    goal.dock_pose = _make_pose_stamped(node, pose)
    goal.dock_type = ""
    goal.navigate_to_staging_pose = True
    return goal


# ─── 어댑터 ─────────────────────────────────────────────────────
class TATAdapter(Node):
    """상위 모듈 명령을 ROS2 action goal로 변환하여 전송 (비블록)."""

    _ACTIONS: Dict[str, ActionSpec] = {
        "navigate": ActionSpec(
            name="/{robot_id}/navigate_to_pose",
            action_type=NavigateToPose,
            goal_builder=_build_navigate_goal,
        ),
        "dock": ActionSpec(
            name="/{robot_id}/dock_robot",
            action_type=DockRobot,
            goal_builder=_build_dock_goal,
        ),
        # "undock": ActionSpec()  # 추가 예정
    }

    def __init__(self, pose_table_path: str | Path, node_name: str = "tat_adapter") -> None:
        super().__init__(node_name)
        self._pose_table: Dict[str, Pose2D] = load_pose_table(pose_table_path)
        self._action_clients: Dict[str, ActionClient] = {}
        # ReentrantCallbackGroup: 여러 액션 콜백이 동시에 fire 되어도 처리 가능.
        # 기본 MutuallyExclusive 면 콜백이 직렬화되어 동시 디스패치 효과가 반감됨.
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

    # ── public ───────────────────────────────────────────────
    def send_command(self,
                     robot_id: str,
                     pose_name: str,
                     action_type: str,
                     wait_server_sec: float = 5.0) -> "PyFuture[SendCommandResult]":
        """
        명령을 비동기로 디스패치하고 즉시 PyFuture 반환.

        호출자 사용법:
          fut = adapter.send_command("TAT1", "ToINSP", "navigate")
          # ... 다른 일 (예: TAT2 명령 디스패치) ...
          result = fut.result(timeout=60)            # 결과를 원하는 시점에 회수
          # 또는 콜백:
          fut.add_done_callback(lambda f: print(f.result()))
        """
        py_future: PyFuture[SendCommandResult] = PyFuture()

        # 1) 입력 검증 (즉시 반환)
        if pose_name not in self._pose_table:
            py_future.set_result(SendCommandResult(succeeded=False,
                                                   message=f"unknown pose_name: {pose_name}"))
            return py_future
        if action_type not in self._ACTIONS:
            py_future.set_result(SendCommandResult(succeeded=False,
                                                   message=f"unsupported action_type: {action_type}"))
            return py_future

        spec = self._ACTIONS[action_type]
        pose = self._pose_table[pose_name]
        action_name = spec.name.format(robot_id=robot_id)

        # 2) ActionClient 준비 (서버 발견은 첫 호출 시에만 실제 대기)
        client = self._get_client(action_name, spec.action_type)
        if not client.wait_for_server(timeout_sec=wait_server_sec):
            py_future.set_result(SendCommandResult(
                succeeded=False,
                message=f"action server unavailable: {action_name}",
            ))
            return py_future

        # 3) goal 송신 → 콜백 체인 (블록 없이 곧장 반환)
        goal_msg = spec.goal_builder(self, pose)
        send_goal_future = client.send_goal_async(goal_msg)
        send_goal_future.add_done_callback(
            lambda f: self._on_goal_response(f, action_name, robot_id, pose_name, action_type, py_future)
        )
        return py_future

    # ── private: 콜백 체인 ───────────────────────────────────
    def _on_goal_response(self,
                          rclpy_fut,
                          action_name: str,
                          robot_id: str,
                          pose_name: str,
                          action_type: str,
                          py_future: "PyFuture[SendCommandResult]") -> None:
        try:
            goal_handle = rclpy_fut.result()
        except Exception as e:
            py_future.set_result(SendCommandResult(succeeded=False,
                                                   message=f"send_goal exception: {e}"))
            return

        if goal_handle is None or not goal_handle.accepted:
            py_future.set_result(SendCommandResult(
                succeeded=False,
                message=f"goal rejected by {action_name}",
            ))
            return

        # goal 수락됨 → 결과 future 에 콜백 다시 등록
        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(
            lambda f: self._on_result(f, robot_id, pose_name, action_type, py_future)
        )

    def _on_result(self,
                   rclpy_fut,
                   robot_id: str,
                   pose_name: str,
                   action_type: str,
                   py_future: "PyFuture[SendCommandResult]") -> None:
        try:
            status = rclpy_fut.result().status
        except Exception as e:
            py_future.set_result(SendCommandResult(succeeded=False,
                                                   message=f"get_result exception: {e}"))
            return

        if status == GoalStatus.STATUS_SUCCEEDED:
            py_future.set_result(SendCommandResult(
                succeeded=True,
                message=f"{robot_id} {action_type} → {pose_name} succeeded",
            ))
        else:
            py_future.set_result(SendCommandResult(
                succeeded=False,
                message=f"{robot_id} {action_type} not succeeded (status={status})",
            ))

    def _get_client(self, action_name: str, action_type: Type) -> ActionClient:
        if action_name not in self._action_clients:
            self._action_clients[action_name] = ActionClient(
                self, action_type, action_name,
                callback_group=self._cb_group,
            )
        return self._action_clients[action_name]