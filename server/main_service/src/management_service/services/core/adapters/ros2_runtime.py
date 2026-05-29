from __future__ import annotations

import threading
from typing import Any


RESOURCE_DOMAIN_MAP: dict[str, int] = {
    "TAT1": 11,
    "TAT2": 12,
    "TAT3": 13,
    "TAT4": 14,
    "TAT5": 15,
    "MAT1": 41,
    "PAT1": 71,
}


class _Ros2Runtime:
    """domain id 하나의 ROS2 runtime."""
    def __init__(self, *, domain_id: int, node_name: str) -> None:
        self.domain_id = int(domain_id)
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


class Ros2RuntimePool:
    """adapter에서 사용해야 하는 ros2 runtime 반환, 없으면 생성."""
    def __init__(self, *, resource_domain_map: dict[str, int] | None = None) -> None:
        self.resource_domain_map = dict(resource_domain_map or RESOURCE_DOMAIN_MAP)
        self._lock = threading.Lock()
        self.runtimes: dict[int, _Ros2Runtime] = {}

    def get_runtime(self, res_id: str) -> _Ros2Runtime:
        domain_id = self.resource_domain_map[res_id]
        with self._lock:
            runtime = self.runtimes.get(domain_id)
            if runtime is None:
                runtime = _Ros2Runtime(
                    domain_id=domain_id,
                    node_name=f"ros2_runtime_domain_{domain_id}",
                )
                self.runtimes[domain_id] = runtime
        runtime.start()
        return runtime

    def shutdown(self) -> None:
        for runtime in self.runtimes.values():
            runtime.shutdown()
        self.runtimes.clear()
