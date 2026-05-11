"""검사 이미지 backend disk 저장 — item_id 단위 폴더 + 회전.

Jetson `UploadInspectionImage` gRPC 가 도착했을 때 본 모듈을 호출하여
`MGMT_INSP_IMAGE_SAVE_DIR/<item_id>/<timestamp>.jpg` 구조로 영속화한다.

설계 원칙:
    - RPC layer 와 분리 — bytes + 메타데이터만 받으면 어디서든 호출 가능
    - 디스크 오류는 warning + None 반환 (호출자가 fallback 결정)
    - 누적 파일은 `MGMT_INSP_IMAGE_MAX_FILES` (기본 20/per-item) 까지만 유지
    - 환경변수 미설정 시 `/var/lib/casting/inspections` 기본 — production 운영 경로
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_ROOT = "/var/lib/casting/inspections"
_DEFAULT_MAX_FILES_PER_ITEM = 20
_TS_PATTERN = "%Y_%m%d_%H%M%S"
_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass(frozen=True)
class SavedInspectionImage:
    """저장 결과 — adapter / RPC 계층이 응답 메시지에 포함."""

    item_id: int
    path: Path
    size_bytes: int
    captured_at_iso: str


class InspectionImageSinkCommand:
    """Backend 측 검사 이미지 영속화 커맨드.

    - root: `MGMT_INSP_IMAGE_SAVE_DIR` env (기본 /var/lib/casting/inspections)
    - rotation: `MGMT_INSP_IMAGE_MAX_FILES` env (기본 20/per-item)
    """

    def __init__(
        self,
        root: str | Path | None = None,
        max_files_per_item: int | None = None,
    ) -> None:
        env_root = root or os.environ.get("MGMT_INSP_IMAGE_SAVE_DIR", _DEFAULT_ROOT)
        self._root = Path(env_root)
        self._max_files = int(
            max_files_per_item
            if max_files_per_item is not None
            else os.environ.get("MGMT_INSP_IMAGE_MAX_FILES", _DEFAULT_MAX_FILES_PER_ITEM)
        )
        logger.info(
            "InspectionImageSinkCommand 초기화: root=%s max_files=%d",
            self._root,
            self._max_files,
        )

    @property
    def root(self) -> Path:
        return self._root

    def save(
        self,
        *,
        item_id: int,
        image_bytes: bytes,
        captured_at: float | None = None,
        label: str | None = None,
    ) -> SavedInspectionImage | None:
        if not image_bytes:
            logger.warning("InspectionImageSinkCommand.save: empty bytes item_id=%s", item_id)
            return None
        if item_id <= 0:
            logger.warning("InspectionImageSinkCommand.save: invalid item_id=%s", item_id)
            return None

        captured_at = captured_at if captured_at and captured_at > 0 else time.time()
        item_dir = self._root / str(item_id)
        try:
            item_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "InspectionImageSinkCommand.save: mkdir 실패 dir=%s exc=%s",
                item_dir,
                exc,
            )
            return None

        target = item_dir / _compose_filename(captured_at, label)
        try:
            target.write_bytes(image_bytes)
        except OSError as exc:
            logger.warning(
                "InspectionImageSinkCommand.save: write 실패 path=%s exc=%s",
                target,
                exc,
            )
            return None

        size = len(image_bytes)
        captured_iso = datetime.utcfromtimestamp(captured_at).strftime("%Y-%m-%dT%H:%M:%SZ")
        logger.info(
            "InspectionImageSinkCommand.save: item_id=%d path=%s bytes=%d",
            item_id,
            target,
            size,
        )

        self._rotate(item_dir)
        return SavedInspectionImage(
            item_id=item_id,
            path=target,
            size_bytes=size,
            captured_at_iso=captured_iso,
        )

    def _rotate(self, item_dir: Path) -> None:
        if self._max_files <= 0:
            return
        try:
            files = sorted(item_dir.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return
        for old in files[self._max_files :]:
            try:
                old.unlink()
                logger.info("InspectionImageSinkCommand.rotate: drop %s", old.name)
            except OSError as exc:
                logger.warning(
                    "InspectionImageSinkCommand.rotate: 실패 %s exc=%s",
                    old.name,
                    exc,
                )


def _compose_filename(captured_at: float, label: str | None) -> str:
    dt = datetime.utcfromtimestamp(captured_at)
    sub = int((captured_at - int(captured_at)) * 10000)
    sub = max(0, min(9999, sub))
    base = dt.strftime(_TS_PATTERN) + f"_{sub:04d}"
    if label:
        sanitized = _SAFE_RE.sub("_", label.strip())[:32]
        if sanitized:
            base = f"{base}_{sanitized}"
    return f"{base}.jpg"
