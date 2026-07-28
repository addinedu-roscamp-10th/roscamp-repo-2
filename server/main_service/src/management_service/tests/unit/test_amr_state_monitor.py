from __future__ import annotations

import asyncio

from services.core.adapters.amr_state_monitor import AmrStateMonitorService


def test_amr_telemetry_is_submitted_without_waiting_for_db() -> None:
    class _StateManager:
        def __init__(self) -> None:
            self.persisted: list[dict[str, int | str]] = []

        async def sync_resource_telemetry(self, telemetry: dict[str, int | str]) -> None:
            self.persisted.append(dict(telemetry))

    state_manager = _StateManager()
    submitted: list[object] = []
    monitor = AmrStateMonitorService(state_manager)
    monitor.set_async_submitter(submitted.append)

    monitor.persist_telemetry("TAT1", battery_pct=73)

    assert len(submitted) == 1
    asyncio.run(submitted[0])
    assert state_manager.persisted == [{"res_id": "TAT1", "battery_pct": 73}]
