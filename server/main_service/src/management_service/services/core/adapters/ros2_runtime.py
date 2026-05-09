from __future__ import annotations

import threading
from typing import Any


class Ros2Runtime:
    """ROS2 action client들이 사용할 멀티스레드 실행환경"""

    def __init__(self, *, node_name: str = "management_ros2_runtime") -> None:
        self.node_name = node_name
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._executor: Any | None = None
        self._rclpy: Any | None = None
        self._owns_context = False
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    def start(self) -> None:
        """thread 시작."""
        with self._lock:
            if self._started:
                return

            try:
                import rclpy
                from rclpy.executors import MultiThreadedExecutor
            except ImportError:
                return

            self._rclpy = rclpy
            if not rclpy.ok():
                rclpy.init(args=None)
                self._owns_context = True

            self._executor = MultiThreadedExecutor()
            self._thread = threading.Thread(
                target=self._executor.spin,
                name=self.node_name,
                daemon=True,
            )
            self._thread.start()
            self._started = True

    def add_node(self, node: Any) -> None:
        """ROS2 노드 추가."""
        if not self._started:
            self.start()
        if self._executor is None:
            raise RuntimeError("ROS2 runtime is not available.")
        self._executor.add_node(node)

    def remove_node(self, node: Any) -> None:
        """종료 시 노드 제거."""
        if self._executor is None:
            return
        self._executor.remove_node(node)

    def shutdown(self) -> None:
        """executor 멈추고 종료."""
        with self._lock:
            if not self._started:
                return

            if self._executor is not None:
                self._executor.shutdown()
            if self._thread is not None:
                self._thread.join(timeout=5.0)

            if self._owns_context and self._rclpy is not None:
                try:
                    self._rclpy.try_shutdown()
                except Exception:
                    pass

            self._executor = None
            self._thread = None
            self._rclpy = None
            self._owns_context = False
            self._started = False
