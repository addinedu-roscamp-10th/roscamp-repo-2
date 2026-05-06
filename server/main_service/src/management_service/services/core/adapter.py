from typing import Any

from services.contracts.protocols import IAdapter


class Adapter(IAdapter):

    async def send_command(self, robot_id: str, action: str, params: dict[str, Any]) -> bool:
        return True
