from __future__ import annotations

import json
from pathlib import Path

from services.contracts.models import AdapterResult


class AIAdapter:

    def execute(
        self,
        item_id: int,
        _robot_id: str,
        _command: str,
        payload: bytes,
    ) -> AdapterResult:
        try:
            payload_dict = json.loads(payload.decode("utf-8")) if payload else {}
        except json.JSONDecodeError:
            return AdapterResult(success=False, message="invalid_json_payload")

        image_path = payload_dict.get("image_path")
        image_url = payload_dict.get("image_url")
        if not image_path and not image_url:
            return AdapterResult(success=False, message="image_path_or_url_required")

        if image_path:
            local_path = Path(image_path)
            if not local_path.exists():
                return AdapterResult(success=False, message=f"image_path_not_found:{image_path}")
            # TODO: Upload slot
            # local_path를 AI 서버에 업로드하고 remote_path/image_url을 받는다.

        inference_payload = {"item_id": item_id, **payload_dict}
        # TODO: Inference request slot
        # inference_payload를 AI 서버에 보내고 검사 결과를 받는다.
        inference_result = {"inference_pending": True}
        return AdapterResult(
            success=True,
            message="ai_request_accepted",
            payload={**inference_payload, "inference": inference_result},
        )

    def close(self) -> None:
        pass
