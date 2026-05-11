"""Hardware streaming RPC methods and image publisher servicer."""

from __future__ import annotations

import logging
import os
import threading
import time

import grpc
import management_pb2  # type: ignore
import management_pb2_grpc  # type: ignore

from services.legacy.command_queue import queue as command_queue

logger = logging.getLogger(__name__)

# 검사 이미지 저장 base 경로. env 로 override 가능 (e.g. /var/lib/casting/inspection_uploads).
_INSP_IMAGE_SAVE_DIR = os.environ.get(
    "MGMT_INSP_IMAGE_SAVE_DIR", "/tmp/inspection_uploads"
)
# UploadInspectionImage idempotency dedup TTL (초). 동일 key 가 본 시간 내 재호출 시 skip.
_INSP_DEDUP_TTL_SECONDS = 60.0
_insp_dedup_lock = threading.Lock()
_insp_dedup_seen: dict[str, tuple[float, str]] = {}


def _insp_dedup_hit(key: str) -> str | None:
    """Idempotency key 중복 검사. hit 면 기존 stored_path 반환, miss 면 None.

    Thread-safe. TTL 만료된 항목은 lazy evict.
    """
    if not key:
        return None
    now = time.time()
    cutoff = now - _INSP_DEDUP_TTL_SECONDS
    with _insp_dedup_lock:
        # lazy evict
        expired = [k for k, (t, _) in _insp_dedup_seen.items() if t < cutoff]
        for k in expired:
            _insp_dedup_seen.pop(k, None)
        entry = _insp_dedup_seen.get(key)
        return entry[1] if entry else None


def _insp_dedup_record(key: str, stored_path: str) -> None:
    if not key:
        return
    with _insp_dedup_lock:
        _insp_dedup_seen[key] = (time.time(), stored_path)


class HardwareRpcMixin:
    """Conveyor command and camera frame stream RPCs."""

    def WatchConveyorCommands(self, request, context):
        subscriber_id = request.subscriber_id or "unknown"
        filter_ = request.robot_id_filter or ""
        logger.info(
            "WatchConveyorCommands subscriber=%s filter=%s",
            subscriber_id,
            filter_ or "<all>",
        )
        while context.is_active():
            cmd = command_queue.wait_next(filter_ or None, timeout=10.0)
            if cmd is None:
                continue
            yield management_pb2.ConveyorCommand(
                robot_id=cmd.robot_id,
                command=cmd.command,
                payload=cmd.payload,
                item_id=cmd.item_id,
                issued_at=management_pb2.Timestamp(iso8601=cmd.issued_at_iso),
                issued_by=cmd.issued_by,
            )
        logger.info("WatchConveyorCommands closed subscriber=%s", subscriber_id)

    def UploadInspectionImage(self, request, context):
        """Jetson Vision Controller 검사 이미지 영구 저장.

        TOF1 카메라 앞 detect 후 캡처된 raw JPEG bytes 를 backend disk 에 저장.
        idempotency_key 로 중복 업로드 차단 (60s TTL).

        env:
            MGMT_INSP_IMAGE_SAVE_DIR  저장 경로 (기본 /tmp/inspection_uploads)
        """
        item_id = int(request.item_id) if request.item_id else 0
        camera_id = (request.camera_id or "unknown").strip() or "unknown"
        stage = (request.stage or "INSP").strip() or "INSP"
        idem = (request.idempotency_key or "").strip()

        if not request.jpeg_bytes:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("jpeg_bytes required")
            return management_pb2.InspectionImageAck(
                accepted=False, stored_path="", reason="empty jpeg_bytes",
            )

        # Idempotency 중복 차단 — 동일 key 재호출 시 기존 stored_path 그대로 반환.
        if idem:
            existing = _insp_dedup_hit(idem)
            if existing:
                logger.info(
                    "UploadInspectionImage: dedup hit idem=%s -> %s", idem, existing,
                )
                return management_pb2.InspectionImageAck(
                    accepted=True,
                    stored_path=existing,
                    reason="duplicate idempotency_key",
                )

        try:
            os.makedirs(_INSP_IMAGE_SAVE_DIR, exist_ok=True)
        except OSError as exc:
            logger.exception(
                "UploadInspectionImage: mkdir 실패 dir=%s", _INSP_IMAGE_SAVE_DIR,
            )
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(f"mkdir error: {exc}")
            return management_pb2.InspectionImageAck(
                accepted=False, stored_path="", reason=str(exc),
            )

        ts_ms = int(time.time() * 1000)
        suffix = f"_{idem}" if idem else ""
        filename = f"item_{item_id}_{stage}_{ts_ms}{suffix}.jpg"
        path = os.path.join(_INSP_IMAGE_SAVE_DIR, filename)

        try:
            with open(path, "wb") as f:
                f.write(request.jpeg_bytes)
        except OSError as exc:
            logger.exception("UploadInspectionImage: disk write 실패 path=%s", path)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(f"disk write error: {exc}")
            return management_pb2.InspectionImageAck(
                accepted=False, stored_path="", reason=str(exc),
            )

        size = len(request.jpeg_bytes)
        _insp_dedup_record(idem, path)
        logger.info(
            "UploadInspectionImage: saved item=%s camera=%s stage=%s size=%dB path=%s",
            item_id, camera_id, stage, size, path,
        )
        return management_pb2.InspectionImageAck(
            accepted=True, stored_path=path, reason="ok",
        )

    def WatchCameraFrames(self, request, context):
        # UI live camera streaming is intentionally disabled.
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("WatchCameraFrames is disabled")
        return

    @staticmethod
    def _frame_to_response(cam: str, frame: dict):
        return management_pb2.CameraFrameResponse(
            available=True,
            camera_id=cam,
            encoding=frame.get("encoding", ""),
            width=int(frame.get("width", 0) or 0),
            height=int(frame.get("height", 0) or 0),
            data=frame.get("data", b""),
            sequence=int(frame.get("sequence", 0) or 0),
            captured_at=management_pb2.Timestamp(iso8601=frame.get("captured_at", "") or ""),
            received_at=management_pb2.Timestamp(iso8601=frame.get("received_at", "") or ""),
        )


class ImagePublisherServicer(management_pb2_grpc.ImagePublisherServiceServicer):
    """HW Image Publishing Service (Jetson) -> Server."""

    def PublishFrames(self, request_iterator, context):
        last_seq = 0
        count = 0
        for frame in request_iterator:
            last_seq = frame.sequence
            count += 1
        logger.info("ImagePublisher 스트림 종료: %d frames, last_seq=%d", count, last_seq)
        return management_pb2.ImageAck(
            sequence=last_seq,
            accepted=True,
            message=f"ignored {count} frames",
        )
