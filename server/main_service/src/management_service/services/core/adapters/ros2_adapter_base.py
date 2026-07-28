from __future__ import annotations

import asyncio
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from services.core.adapters.ros2_runtime import Ros2Runtime


class BaseRos2Adapter:
    """ros2 adapter base"""

    _runtime: Ros2Runtime | None
    _node_cls: Any | None
    _action_client_cls: Any | None
    _node: Any | None
    _clients: dict[str, Any]
    _started: bool

    def __init__(self, runtime: Ros2Runtime | None = None) -> None:
        self._runtime = runtime
        self._node_cls = None
        self._action_client_cls = None
        self._node = None
        self._clients = {}
        self._started = False

    @property
    def _adapter_node_name(self) -> str:
        return f"{type(self).__name__.lower()}_node"

    def _get_node(self) -> Any | None:
        if self._node is not None:
            return self._node
        if self._runtime is None or self._node_cls is None:
            return None

        if not self._runtime.started:
            self._runtime.start()
        if not self._runtime.started or self._runtime.context is None:
            return None

        node = self._node_cls(
            self._adapter_node_name,
            context=self._runtime.context,
        )
        self._runtime.add_node(node)
        self._node = node
        return node

    def _get_or_create_client(
        self,
        action_name: str,
        action_type: Any,
    ) -> Any | None:
        if action_name in self._clients:
            return self._clients[action_name]
        if self._action_client_cls is None:
            return None

        node = self._get_node()
        if node is None:
            return None
        client = self._action_client_cls(node, action_type, action_name)
        self._clients[action_name] = client
        return client

    async def _send_single_goal_async(
        self,
        client: Any,
        goal: Any,
        parse_result: Callable[[Any], tuple[bool, str]],
        *,
        prefix: str,
        action_name: str,
        rejected_tag: str | None = None,
        timeout_tag: str | None = None,
        wait_sec: float = 5.0,
        timeout_sec: float = 300.0,
    ) -> tuple[bool, str]:
        rejected_tag = rejected_tag or action_name
        timeout_tag = timeout_tag or action_name

        if not await asyncio.to_thread(client.wait_for_server, wait_sec):
            return (False, f"{prefix}_action_server_unavailable:{action_name}")

        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[bool, str]] = loop.create_future()

        def set_result(value: tuple[bool, str]) -> None:
            if not future.done():
                future.set_result(value)

        def result_callback(rclpy_future: Any) -> None:
            try:
                value = parse_result(rclpy_future.result())
            except Exception as exc:
                value = (False, f"{prefix}_result_exception:{exc}")
            loop.call_soon_threadsafe(set_result, value)

        def goal_response_callback(rclpy_future: Any) -> None:
            try:
                goal_handle = rclpy_future.result()
            except Exception as exc:
                loop.call_soon_threadsafe(
                    set_result, (False, f"{prefix}_goal_exception:{exc}")
                )
                return
            if goal_handle is None or not goal_handle.accepted:
                loop.call_soon_threadsafe(
                    set_result, (False, f"{prefix}_goal_rejected:{rejected_tag}")
                )
                return
            goal_handle.get_result_async().add_done_callback(result_callback)

        client.send_goal_async(goal).add_done_callback(goal_response_callback)

        try:
            return await asyncio.wait_for(future, timeout=timeout_sec)
        except asyncio.TimeoutError:
            return (False, f"{prefix}_action_timeout:{timeout_tag}")

    async def _call_service_async(
        self,
        service_name: str,
        service_type: Any,
        request: Any,
        timeout_sec: float = 5.0,
    ) -> Any:
        node = self._get_node()
        if node is None:
            raise RuntimeError("node not started")

        client = node.create_client(service_type, service_name)
        try:
            if not await asyncio.to_thread(client.wait_for_service, timeout_sec):
                raise RuntimeError(f"Service {service_name} not available")

            loop = asyncio.get_running_loop()
            asyncio_future = loop.create_future()

            def done_callback(rclpy_future: Any) -> None:
                try:
                    res = rclpy_future.result()
                    loop.call_soon_threadsafe(asyncio_future.set_result, res)
                except Exception as exc:
                    loop.call_soon_threadsafe(asyncio_future.set_exception, exc)

            client.call_async(request).add_done_callback(done_callback)
            return await asyncio.wait_for(asyncio_future, timeout=timeout_sec)
        finally:
            node.destroy_client(client)

    def close(self) -> None:
        self._clients.clear()
        if self._node is not None and self._runtime is not None:
            try:
                self._runtime.remove_node(self._node)
                self._node.destroy_node()
            except Exception:
                pass
        self._node = None
        self._started = False
