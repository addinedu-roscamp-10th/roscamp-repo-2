from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from services.contracts.models import AdapterResult
from services.contracts.protocols import IAdapter
from services.core.adapters.ai_adapter import AIAdapter
from services.core.adapters.conv_adapter import ConvAdapter
from services.core.adapters.mat_adapter import MatAdapter
from services.core.adapters.pat_adapter import PatAdapter
from services.core.adapters.ros2_runtime import Ros2Runtime
from services.core.adapters.tat_adapter import TAT_DOCK_ACTION, TATAdapter

logger = logging.getLogger(__name__)


class AdapterRouter(IAdapter):
    def __init__(
        self,
        ros2_runtime: Ros2Runtime | None = None,
    ) -> None:
        self._ros2_runtime = ros2_runtime
        self._mat_adapter = MatAdapter(ros2_runtime=ros2_runtime)
        self._pat_adapter = PatAdapter(ros2_runtime=ros2_runtime)
        self._tat_adapter = TATAdapter(ros2_runtime=ros2_runtime)
        self._conv_adapter = ConvAdapter()
        self._ai_adapter = AIAdapter()

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
        """adapter command 진입점. action params 정규화 후 res별 adapter로 라우팅."""
        payload = dict(params or {})
        payload_bytes = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        item_id = int(payload.get("item_id", 0) or 0)
        normalized_id = (res_id or "").upper()

        # AI
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

        # MAT
        if (
            self._ros2_runtime is not None
            and normalized_id == "MAT"
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

        # PAT
        if (
            self._ros2_runtime is not None
            and normalized_id == "PAT"
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
        # CONV
        if normalized_id == "CONV1":
            ok, message = self._conv_adapter.execute(item_id, res_id, action, payload_bytes)
            logger.info("Adapter routed to CONV: action=%s res_id=%s result=%s", action, res_id, message)
            return AdapterResult(success=ok, message=message)

        # TAT
        if (
            self._ros2_runtime is not None
            and normalized_id.startswith("TAT")
            and action == TAT_DOCK_ACTION
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

        logger.warning("Adapter unsupported command: action=%s res_id=%s", action, res_id)
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
