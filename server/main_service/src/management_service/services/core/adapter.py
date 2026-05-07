from typing import Any

from services.contracts.protocols import IAdapter


class Adapter(IAdapter):

    async def send_command(self, robot_id: str, action: str, params: dict[str, Any]) -> bool:
        """상위 로직 Test를 위해 return True로 설정"""
        return True
