"""검사 이미지 디스크 저장 — item_id 별 폴더 + 회전 정책.

설계 원칙:
    - 외부에서 받은 raw bytes 를 Inspection_Image/<item_id>/<timestamp>.jpg 로 저장
    - 디렉터리는 lazy create (필요할 때만 mkdir)
    - 디스크 오류는 warning 로깅 + None 반환 (호출자가 polic 결정)
    - 같은 item 의 누적 파일 수는 AI_INSP_MAX_FILES (기본 50) 로 제한
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

_DEFAULT_ROOT = "Inspection_Image"
_DEFAULT_MAX_FILES_PER_ITEM = 50
_TS_PATTERN = "%Y_%m%d_%H%M%S"
_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass(frozen=True)
class StoredImage:
    """디스크 저장 결과 — 호출자가 응답 payload 에 포함."""

    item_id: int
    path: Path
    size_bytes: int
    captured_at_iso: str


class InspectionImageStore:
    """AI 서버 측 검사 이미지 저장소.

    - 기본 root: `AI_INSP_IMAGE_ROOT` env (기본 ./Inspection_Image)
    - 회전 한도: `AI_INSP_MAX_FILES` env (기본 50/per-item)
    """

    def __init__(self, root: str | Path | None = None, max_files_per_item: int | None = None) -> None:
        env_root = root or os.environ.get("AI_INSP_IMAGE_ROOT", _DEFAULT_ROOT)
        self._root = Path(env_root)
        self._max_files = int(
            max_files_per_item
            if max_files_per_item is not None
            else os.environ.get("AI_INSP_MAX_FILES", _DEFAULT_MAX_FILES_PER_ITEM)
        )
        logger.info(
            "InspectionImageStore 초기화: root=%s max_files=%d",
            self._root.resolve(),
            self._max_files,
        )

    def save(
        self,
        *,
        item_id: int,
        image_bytes: bytes,
        captured_at: float | None = None,
        label: str | None = None,
    ) -> StoredImage | None:
        if not image_bytes:
            logger.warning("InspectionImageStore.save: empty image_bytes item_id=%s", item_id)
            return None
        if item_id <= 0:
            logger.warning("InspectionImageStore.save: invalid item_id=%s", item_id)
            return None

        captured_at = captured_at if captured_at and captured_at > 0 else time.time()
        item_dir = self._root / str(item_id)
        try:
            item_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("InspectionImageStore.save: mkdir 실패 dir=%s exc=%s", item_dir, exc)
            return None

        filename = _compose_filename(captured_at, label)
        target = item_dir / filename
        try:
            target.write_bytes(image_bytes)
        except OSError as exc:
            logger.warning("InspectionImageStore.save: write 실패 path=%s exc=%s", target, exc)
            return None

        size = len(image_bytes)
        captured_iso = datetime.utcfromtimestamp(captured_at).strftime("%Y-%m-%dT%H:%M:%SZ")
        logger.info(
            "InspectionImageStore.save: item_id=%d path=%s bytes=%d",
            item_id,
            target,
            size,
        )

        self._rotate(item_dir)
        return StoredImage(
            item_id=item_id,
            path=target,
            size_bytes=size,
            captured_at_iso=captured_iso,
        )

    def _rotate(self, item_dir: Path) -> None:
        """item_dir 내 .jpg 파일이 max_files 초과 시 mtime 오래된 것부터 삭제."""
        if self._max_files <= 0:
            return
        try:
            files = sorted(item_dir.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return
        for old in files[self._max_files :]:
            try:
                old.unlink()
                logger.info("InspectionImageStore.rotate: drop %s", old.name)
            except OSError as exc:
                logger.warning("InspectionImageStore.rotate: 실패 %s exc=%s", old.name, exc)


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
