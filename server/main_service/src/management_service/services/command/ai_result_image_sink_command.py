"""AI 검사 결과 이미지 backend disk 저장 — segmented + result 두 장.

AI 서버 `/predict` 응답의 `segmented_image` / `result_image` (base64 PNG) 을 받아
`MGMT_INSP_IMAGE_SAVE_DIR/<item_id>/<insp_txn_id>_segmented.png`,
`MGMT_INSP_IMAGE_SAVE_DIR/<item_id>/<insp_txn_id>_result.png` 로 영속화하고
HttpImageServer 가 노출하는 외부 fetch URL 을 반환한다.

설계 원칙:
    - RPC layer / AI command 와 분리 — base64 문자열만 받으면 어디서든 호출 가능
    - 디스크 오류 / base64 디코드 실패는 warning + None URL 반환 (호출자가 inference 자체는 SUCC 유지)
    - URL 합성: `{MGMT_IMAGE_BASE_URL}/inspections/{item_id}/{filename}`
      (default base url = http://127.0.0.1:18800 — HttpImageServer 와 정합)
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_ROOT = "/var/lib/casting/inspections"
_DEFAULT_BASE_URL_HOST = "127.0.0.1"
_DEFAULT_HTTP_PORT = "18800"


@dataclass(frozen=True)
class SavedAiResultImages:
    """저장 결과 — record_inspection_result 가 DB 영속화 시 사용."""

    item_id: int
    insp_txn_id: int
    segmented_path: Path | None
    segmented_url: str | None
    result_path: Path | None
    result_url: str | None


class AiResultImageSinkCommand:
    """AI /predict 응답 base64 이미지 디스크 저장 + URL 합성.

    - root: `MGMT_INSP_IMAGE_SAVE_DIR` env (기본 /var/lib/casting/inspections)
    - base_url: `MGMT_IMAGE_BASE_URL` env (기본 http://127.0.0.1:18800)
        - host override: `MGMT_IMAGE_BASE_HOST` (HttpImageServer 와 동일 env)
        - port override: `MGMT_IMAGE_HTTP_PORT` (HttpImageServer 와 동일 env)
    """

    def __init__(
        self,
        root: str | Path | None = None,
        base_url: str | None = None,
    ) -> None:
        env_root = root or os.environ.get("MGMT_INSP_IMAGE_SAVE_DIR", _DEFAULT_ROOT)
        self._root = Path(env_root)
        if base_url:
            self._base_url = base_url.rstrip("/")
        else:
            self._base_url = self._compose_default_base_url().rstrip("/")
        logger.info(
            "AiResultImageSinkCommand 초기화: root=%s base_url=%s",
            self._root,
            self._base_url,
        )

    @property
    def root(self) -> Path:
        return self._root

    @property
    def base_url(self) -> str:
        return self._base_url

    def save(
        self,
        *,
        item_id: int,
        insp_txn_id: int,
        segmented_image_b64: str | None,
        result_image_b64: str | None,
    ) -> SavedAiResultImages:
        """두 base64 이미지를 디스크에 저장하고 URL 을 반환.

        한쪽이 None / 디코드 실패해도 다른 한쪽은 정상 저장 시도. 호출자가
        None URL 을 그대로 영속화하면 DB 컬럼은 NULL 로 기록된다.
        """
        if item_id <= 0 or insp_txn_id <= 0:
            logger.warning(
                "AiResultImageSinkCommand.save: invalid id item_id=%s insp_txn_id=%s",
                item_id, insp_txn_id,
            )
            return SavedAiResultImages(
                item_id=item_id,
                insp_txn_id=insp_txn_id,
                segmented_path=None,
                segmented_url=None,
                result_path=None,
                result_url=None,
            )

        item_dir = self._root / str(item_id)
        try:
            item_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "AiResultImageSinkCommand.save: mkdir 실패 dir=%s exc=%s",
                item_dir, exc,
            )
            return SavedAiResultImages(
                item_id=item_id,
                insp_txn_id=insp_txn_id,
                segmented_path=None,
                segmented_url=None,
                result_path=None,
                result_url=None,
            )

        seg_path, seg_url = self._save_one(
            item_dir=item_dir,
            item_id=item_id,
            filename=f"{insp_txn_id}_segmented.png",
            b64=segmented_image_b64,
            label="segmented",
        )
        res_path, res_url = self._save_one(
            item_dir=item_dir,
            item_id=item_id,
            filename=f"{insp_txn_id}_result.png",
            b64=result_image_b64,
            label="result",
        )

        return SavedAiResultImages(
            item_id=item_id,
            insp_txn_id=insp_txn_id,
            segmented_path=seg_path,
            segmented_url=seg_url,
            result_path=res_path,
            result_url=res_url,
        )

    # ---------- internal -----------------------------------------------------
    def _save_one(
        self,
        *,
        item_dir: Path,
        item_id: int,
        filename: str,
        b64: str | None,
        label: str,
    ) -> tuple[Path | None, str | None]:
        if not b64:
            return (None, None)

        # data URI prefix("data:image/png;base64,...") 가 섞여 들어오는 경우 보호적으로 strip.
        payload = b64.split(",", 1)[1] if b64.startswith("data:") and "," in b64 else b64
        try:
            decoded = base64.b64decode(payload, validate=False)
        except (binascii.Error, ValueError) as exc:
            logger.warning(
                "AiResultImageSinkCommand: base64 decode 실패 label=%s item_id=%s exc=%s",
                label, item_id, exc,
            )
            return (None, None)

        if not decoded:
            logger.warning(
                "AiResultImageSinkCommand: empty decoded bytes label=%s item_id=%s",
                label, item_id,
            )
            return (None, None)

        target = item_dir / filename
        try:
            target.write_bytes(decoded)
        except OSError as exc:
            logger.warning(
                "AiResultImageSinkCommand: write 실패 label=%s path=%s exc=%s",
                label, target, exc,
            )
            return (None, None)

        url = f"{self._base_url}/inspections/{item_id}/{filename}"
        logger.info(
            "AiResultImageSinkCommand: 저장 label=%s item_id=%d insp_txn=?, path=%s bytes=%d url=%s",
            label, item_id, target, len(decoded), url,
        )
        return (target, url)

    @staticmethod
    def _compose_default_base_url() -> str:
        host = os.environ.get("MGMT_IMAGE_BASE_HOST", _DEFAULT_BASE_URL_HOST).strip() or _DEFAULT_BASE_URL_HOST
        port = os.environ.get("MGMT_IMAGE_HTTP_PORT", _DEFAULT_HTTP_PORT).strip() or _DEFAULT_HTTP_PORT
        return f"http://{host}:{port}"
