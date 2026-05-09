from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.contracts.protocols import IStateManager


# PyQt 로봇 상태 조회 포맷?
@dataclass(frozen=True)
class AmrStatus:
    id: str
    host: str = "ros2"
    status: str = "online"
    battery: float = 0.0
    voltage: float = 0.0
    location: str = "-"
    task_state: int = 1
    task_id: str = ""
    loaded_item: str = ""

class AmrStateMonitorNode:
    """AMR 배터리 및 amcl_pose topic 구독"""

    def __init__(self, state_manager: IStateManager, service: "AmrStateMonitorService"):
        import rclpy
        from geometry_msgs.msg import Pose
        from rclpy.node import Node
        from std_msgs.msg import Float32

        class _Node(Node):
            def __init__(self) -> None:
                super().__init__("amr_state_monitor")

                self.declare_parameter("robots", ["TAT1", "TAT2", "TAT3"])
                self.robots = list(self.get_parameter("robots").value)
                self.status = {
                    robot: {"percent": None, "position": None}
                    for robot in self.robots
                }
                self.amr_state_subscriptions = []

                for robot in self.robots:
                    percent_topic = f"/{robot}/battery/percent"
                    amcl_topic = f"/{robot}/amcl_pose"

                    self.amr_state_subscriptions.append(
                        self.create_subscription(
                            Float32,
                            percent_topic,
                            lambda msg, robot_name=robot: self.percentage_callback(robot_name, msg),
                            10,
                        )
                    )
                    self.amr_state_subscriptions.append(
                        self.create_subscription(
                            Pose,
                            amcl_topic,
                            lambda msg, robot_name=robot: self.amcl_callback(robot_name, msg),
                            10,
                        )
                    )

            def percentage_callback(self, robot_name, msg) -> None:
                """배터리% 콜백함수."""
                percent = float(msg.data)
                self.status[robot_name]["percent"] = percent
                service.update_status(robot_name, battery=percent) # state manager에 배터리 상태 저장
                state_manager.update_amr_runtime_memory(
                    robot_name.strip(),
                    battery_pct=int(round(percent)),
                )

            def amcl_callback(self, robot_name, msg) -> None:
                """좌표 콜백함수."""
                x = float(msg.position.x)
                y = float(msg.position.y)
                self.status[robot_name]["position"] = [x, y]
                service.update_status(robot_name, x=x, y=y) # state manager에 좌표 저장
                state_manager.update_amr_runtime_memory(
                    robot_name.strip(),
                    x=x,
                    y=y,
                )

        self._rclpy = rclpy
        self.node = _Node()

    def spin_once(self, timeout_sec: float = 0.5) -> None:
        self._rclpy.spin_once(self.node, timeout_sec=timeout_sec)

    def destroy(self) -> None:
        self.node.destroy_node()


class AmrStateMonitorService:

    def __init__(self, state_manager: IStateManager) -> None:
        self._state_manager = state_manager
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._node: AmrStateMonitorNode | None = None
        self._lock = threading.Lock()
        self._cache: dict[str, AmrStatus] = {}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="amr-state-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def get_all(self) -> list[AmrStatus]:
        with self._lock:
            return list(self._cache.values())

    def update_status(
        self,
        robot_name: str,
        *,
        battery: float | None = None,
        x: float | None = None,
        y: float | None = None,
    ) -> None:
        res_id = robot_name.strip()
        with self._lock:
            current = self._cache.get(res_id, AmrStatus(id=res_id))
            location = current.location
            if x is not None and y is not None:
                location = f"x={x:.2f}, y={y:.2f}"
            self._cache[res_id] = AmrStatus(
                id=res_id,
                host=current.host,
                status="online",
                battery=current.battery if battery is None else battery,
                voltage=current.voltage,
                location=location,
                task_state=current.task_state,
                task_id=current.task_id,
                loaded_item=current.loaded_item,
            )

    def _run(self) -> None:
        try:
            import rclpy
        except ImportError:
            return

        owns_context = False
        try:
            if not rclpy.ok():
                rclpy.init(args=None)
                owns_context = True
            self._node = AmrStateMonitorNode(self._state_manager, self)
            while not self._stop.is_set() and rclpy.ok():
                self._node.spin_once(timeout_sec=0.5)
        except Exception:
            pass
        finally:
            if self._node is not None:
                self._node.destroy()
                self._node = None
            if owns_context:
                try:
                    rclpy.try_shutdown()
                except Exception:
                    pass
