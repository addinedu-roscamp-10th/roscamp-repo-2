from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from services.core.adapters.ros2_runtime import Ros2RuntimePool


@dataclass
class _DomainSession:
    runtime: Any
    node: Any
    clients: dict[str, Any] = field(default_factory=dict)


class BaseRos2Adapter:
    """ros2 adapter base"""

    _runtime_pool: Ros2RuntimePool | None
    _node_cls: Any | None
    _action_client_cls: Any | None
    _sessions: dict[str, _DomainSession]
    _started: bool

    def __init__(self, runtime_pool: Ros2RuntimePool | None = None) -> None:
        self._runtime_pool = runtime_pool
        self._node_cls = None
        self._action_client_cls = None
        self._sessions = {}
        self._started = False

    def _session_for(self, res_id: str) -> _DomainSession | None:
        session = self._sessions.get(res_id)
        if session is not None:
            return session
        if self._runtime_pool is None or self._node_cls is None:
            return None

        try:
            runtime = self._runtime_pool.get_runtime(res_id)
        except KeyError:
            return None
        if not runtime.started or runtime.context is None:
            return None

        node = self._node_cls(f"{res_id.lower()}_adapter", context=runtime.context)
        runtime.add_node(node)
        session = _DomainSession(runtime=runtime, node=node)
        self._sessions[res_id] = session
        return session

    def _get_or_create_client(
        self,
        session: _DomainSession,
        action_name: str,
        action_type: Any,
    ) -> Any | None:
        if action_name in session.clients:
            return session.clients[action_name]
        if self._action_client_cls is None:
            return None
        client = self._action_client_cls(
            session.node,
            action_type,
            action_name,
        )
        session.clients[action_name] = client
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

    def close(self) -> None:
        for session in self._sessions.values():
            try:
                session.runtime.remove_node(session.node)
                session.node.destroy_node()
            except Exception:
                pass
            session.clients.clear()
        self._sessions.clear()
        self._started = False
