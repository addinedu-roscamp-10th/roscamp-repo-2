from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from services.contracts.enums import ResourceType
from services.contracts.models import AdapterResult
from services.contracts.protocols import IAdapter, IStateManager
from services.core.adapters.ai_adapter import AIAdapter
from services.core.adapters.conv_adapter import ConvAdapter
from services.core.adapters.mat_adapter import MatAdapter
from services.core.adapters.pat_adapter import PatAdapter
from services.core.adapters.ros2_runtime import Ros2Runtime
from services.core.adapters.tat_adapter import TATAdapter

logger = logging.getLogger(__name__)


class AdapterRouter(IAdapter):
    def __init__(
        self,
        ros2_runtime: Ros2Runtime | None = None,
        *,
        state_manager: IStateManager | None = None,
    ) -> None:
        self._ros2_runtime = ros2_runtime
        self._state_manager = state_manager
        self._mat_adapter = MatAdapter(ros2_runtime=ros2_runtime)
        self._pat_adapter = PatAdapter(ros2_runtime=ros2_runtime)
        self._tat_adapter = TATAdapter(ros2_runtime=ros2_runtime)
        self._conv_adapter = ConvAdapter()
        self._ai_adapter = AIAdapter()

    def _resolve_res_type(self, res_id: str) -> str | None:
        """master(res 테이블) 기반의 res_type 조회. state_manager가 없거나 미등록
        res_id면 id 패턴으로 폴백 (TAT*, MAT, PAT, CONV*)."""
        if self._state_manager is not None:
            get_res_type = getattr(self._state_manager, "get_res_type", None)
            if get_res_type is not None:
                rt = get_res_type(res_id)
                if rt:
                    return rt
        upper = (res_id or "").upper()
        if upper in {ResourceType.MAT.value, ResourceType.PAT.value}:
            return upper
        if upper.startswith(ResourceType.TAT.value):
            return ResourceType.TAT.value
        if upper.startswith(ResourceType.CONV.value):
            return ResourceType.CONV.value
        return None

    def start(self) -> None:
        """등록된 모든 어댑터를 초기화하고 시작."""
        for adapter in (
            self._mat_adapter,
            self._pat_adapter,
            self._tat_adapter,
            self._conv_adapter,
            self._ai_adapter,
        ):
            start = getattr(adapter, "start", None)
            if start is not None:
                start()

    async def send_command(self, res_id: str, action: str, params: dict[str, Any]) -> AdapterResult:
        # return AdapterResult(success=True, message="") # 테스트용 true 반환기, 사용 시 아래 주석처리
        """adapter command 진입점. res_type(master)으로 어댑터를 선택해 라우팅."""
        payload = dict(params or {})
        payload_bytes = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        item_id = int(payload.get("item_id", 0) or 0)

        # AI는 res_id와 무관, action으로만 분기
        if action == "AI_INFERENCE_REQUEST":
            result = await asyncio.to_thread(
                self._ai_adapter.execute,
                item_id,
                res_id,
                action,
                payload_bytes,
            )
            logger.info("Adapter routed to AI: action=%s res_id=%s result=%s", action, res_id, result.message)
            return result

        res_type = self._resolve_res_type(res_id)

        if (
            res_type == ResourceType.MAT.value
            and self._ros2_runtime is not None
            and self._mat_adapter.supports(action)
        ):
            ok, message = await asyncio.to_thread(
                self._mat_adapter.execute,
                item_id,
                res_id,
                action,
                payload_bytes,
            )
            logger.info("Adapter routed to MAT: action=%s res_id=%s result=%s", action, res_id, message)
            return AdapterResult(success=ok, message=message)

        if (
            res_type == ResourceType.PAT.value
            and self._ros2_runtime is not None
            and self._pat_adapter.supports(action)
        ):
            ok, message = await asyncio.to_thread(
                self._pat_adapter.execute,
                item_id,
                res_id,
                action,
                payload_bytes,
            )
            logger.info("Adapter routed to PAT: action=%s res_id=%s result=%s", action, res_id, message)
            return AdapterResult(success=ok, message=message)

        if res_type == ResourceType.CONV.value:
            ok, message = self._conv_adapter.execute(item_id, res_id, action, payload_bytes)
            logger.info("Adapter routed to CONV: action=%s res_id=%s result=%s", action, res_id, message)
            return AdapterResult(success=ok, message=message)

        if (
            res_type == ResourceType.TAT.value
            and self._ros2_runtime is not None
            and self._tat_adapter.supports(action)
        ):
            ok, message = await asyncio.to_thread(
                self._tat_adapter.execute,
                item_id,
                res_id,
                action,
                payload_bytes,
            )
            logger.info("Adapter routed to TAT: action=%s res_id=%s result=%s", action, res_id, message)
            return AdapterResult(success=ok, message=message)

        logger.warning("Adapter unsupported command: action=%s res_id=%s res_type=%s", action, res_id, res_type)
        return AdapterResult(success=False, message=f"unsupported_command:{action}")

    def close(self) -> None:
        for adapter in (
            self._mat_adapter,
            self._pat_adapter,
            self._tat_adapter,
            self._conv_adapter,
            self._ai_adapter,
        ):
            close = getattr(adapter, "close", None)
            if close is not None:
                close()
