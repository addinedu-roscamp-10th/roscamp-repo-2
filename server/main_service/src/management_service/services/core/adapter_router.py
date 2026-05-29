from __future__ import annotations

import logging
from typing import Any

from services.contracts.enums import ResourceType
from services.contracts.models import AdapterResult
from services.contracts.protocols import IAdapter
from services.core.adapters.ai_adapter import AIAdapter
from services.core.adapters.conv_adapter import ConvAdapter
from services.core.adapters.mat_adapter import MatAdapter
from services.core.adapters.pat_adapter import PatAdapter
from services.core.adapters.ros2_runtime import Ros2RuntimePool
from services.core.adapters.tat_adapter import TATAdapter

logger = logging.getLogger(__name__)


class AdapterRouter(IAdapter):
    """각 res_id와 action에 맞는 adapter로 라우팅"""

    def __init__(
        self,
        runtime_pool: Ros2RuntimePool | None = None,
    ) -> None:
        self._runtime_pool = runtime_pool
        self._mat_adapter = MatAdapter(runtime_pool=runtime_pool)
        self._pat_adapter = PatAdapter(runtime_pool=runtime_pool)
        self._tat_adapter = TATAdapter(runtime_pool=runtime_pool)
        self._conv_adapter = ConvAdapter()
        self._ai_adapter = AIAdapter()
        self._adapters: dict[str, IAdapter] = {
            ResourceType.MAT.value: self._mat_adapter,
            ResourceType.PAT.value: self._pat_adapter,
            ResourceType.TAT.value: self._tat_adapter,
            ResourceType.CONV.value: self._conv_adapter,
        }

    def _resolve_res_type(self, res_id: str) -> str | None:
        # res_id에서 prefix만 남기기
        res_type = res_id.upper().rstrip("0123456789-")
        return res_type if res_type in self._adapters else None

    def start(self) -> None:
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
        # AI는 action명으로 라우팅
        if action == "AI_INFERENCE_REQUEST":
            result = await self._ai_adapter.send_command(res_id, action, params)
            logger.info("Adapter routed to AI: action=%s res_id=%s result=%s", action, res_id, result.message)
            return result

        res_type = self._resolve_res_type(res_id)
        adapter = self._adapters.get(res_type) if res_type else None
        if adapter is None:
            logger.warning(
                "Adapter unsupported command: action=%s res_id=%s res_type=%s",
                action, res_id, res_type,
            )
            return AdapterResult(success=False, message=f"unsupported_command:{action}")

        result = await adapter.send_command(res_id, action, params)
        logger.info(
            "Adapter routed to %s: action=%s res_id=%s result=%s",
            res_type, action, res_id, result.message,
        )
        return result

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
