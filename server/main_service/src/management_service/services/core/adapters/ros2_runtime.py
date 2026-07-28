from __future__ import annotations

import os
import threading
from typing import Any


MANAGEMENT_DOMAIN_ID: int = int(os.environ.get("ROS_DOMAIN_ID", "100"))


class Ros2Runtime:
    """단일 ROS2 runtime."""

    def __init__(
        self,
        *,
        domain_id: int = MANAGEMENT_DOMAIN_ID,
        node_name: str = "management_service",
    ) -> None:
        self.domain_id = domain_id
        self.node_name = node_name
        self._lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.executor: Any | None = None
        self.context: Any | None = None
        self.nodes: list[Any] = []
        self.started = False

    def start(self) -> None:
        with self._lock:
            if self.started:
                return

            try:
                import rclpy
                from rclpy.context import Context
                from rclpy.executors import SingleThreadedExecutor
            except ImportError:
                return

            context = Context()
            rclpy.init(args=None, context=context, domain_id=self.domain_id)
            executor = SingleThreadedExecutor(context=context)
            thread = threading.Thread(
                target=executor.spin,
                name=self.node_name,
                daemon=True,
            )
            thread.start()

            self.context = context
            self.executor = executor
            self.thread = thread
            self.started = True

    def add_node(self, node: Any) -> None:
        if not self.started:
            self.start()
        if self.executor is None:
            raise RuntimeError(f"ROS2 runtime is not available for domain {self.domain_id}.")
        self.executor.add_node(node)
        self.nodes.append(node)

    def remove_node(self, node: Any) -> None:
        if self.executor is None:
            return
        self.executor.remove_node(node)
        try:
            self.nodes.remove(node)
        except ValueError:
            pass

    def shutdown(self) -> None:
        with self._lock:
            if not self.started:
                return

            if self.executor is not None:
                self.executor.shutdown()
            if self.thread is not None:
                self.thread.join(timeout=5.0)
            for node in self.nodes:
                try:
                    node.destroy_node()
                except Exception:
                    pass
            self.nodes.clear()
            if self.context is not None:
                try:
                    self.context.try_shutdown()
                except Exception:
                    pass

            self.thread = None
            self.executor = None
            self.context = None
            self.started = False
