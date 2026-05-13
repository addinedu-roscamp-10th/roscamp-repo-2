"""Hardware streaming RPC methods and image publisher servicer.

이 파일은 PR #9 (UploadInspectionImage RPC + dedup + disk save) 와
본 PR (AI inspection chain wiring) 의 통합 형태이다. PR #9 가 먼저 dev 에
머지될 경우 본 파일과 conflict 가 발생하며, conflict 해결 시 "ours"
(InspectionImageSinkCommand + AI 어댑터 dispatch 포함된 본 버전) 을 채택하면
e2e 사슬이 자동 트리거된다.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import grpc
import management_pb2  # type: ignore
import management_pb2_grpc  # type: ignore

from services.command.inspection_image_sink_command import InspectionImageSinkCommand
from services.legacy.command_queue import queue as command_queue

logger = logging.getLogger(__name__)

# UploadInspectionImage idempotency dedup TTL (초). 동일 key 가 본 시간 내 재호출 시 skip.
_INSP_DEDUP_TTL_SECONDS = 60.0
_insp_dedup_lock = threading.Lock()
_insp_dedup_seen: dict[str, tuple[float, str]] = {}

# 검사 이미지 sink — InspectionImageSinkCommand 가 `MGMT_INSP_IMAGE_SAVE_DIR/<item_id>/<ts>.jpg`
# 구조로 영속화 + 회전. 모듈 전역 인스턴스 1개만 사용한다 (env 변경은 재시작 시 반영).
_image_sink = InspectionImageSinkCommand()


def _insp_dedup_hit(key: str) -> str | None:
    """Idempotency key 중복 검사. hit 면 기존 stored_path 반환, miss 면 None.

    Thread-safe. TTL 만료된 항목은 lazy evict.
    """
    if not key:
        return None
    now = time.time()
    cutoff = now - _INSP_DEDUP_TTL_SECONDS
    with _insp_dedup_lock:
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


def _dispatch_ai_inference(
    *, item_id: int, image_path: str, captured_at: float, label: str
) -> None:
    """AI 어댑터로 비동기 dispatch — gRPC handler 가 즉시 응답하도록 백그라운드 스레드 사용.

    AI 호출 + DB 기록 (insp_task_txn SUCC + ai_inference_txn + insp_stat + item) 은
    ai_adapter.execute() 안에서 일어난다. INSP_COMPLETED publish 는 이 시점이 아니라
    ToPAWait/CONV_ALLOW_MOVE 단계 (conv_adapter) 에서 발생하여 AMR 도착 후에만
    컨베이어가 RUN 하도록 한다 (commit 2593b9e). 실패 path 에서는 ai_adapter 가
    inspection_result_command.record_inspection_failure 를 호출해 insp_task_txn 을
    FAIL 로 마감한다.
    """
    try:
        # 지연 import — container 초기화 순서 의존성 (server.py 가 import 시 container 가 준비됨)
        from container import container
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "UploadInspectionImage: container import 실패 — AI dispatch skip (%s)", exc,
        )
        return

    payload = json.dumps(
        {
            "image_path": image_path,
            "item_id": item_id,
            "captured_at": captured_at,
            "label": label,
        },
        ensure_ascii=True,
    ).encode("utf-8")

    def _run() -> None:
        try:
            adapter = container.adapter._ai_adapter  # noqa: SLF001  (AdapterRouter 내부 어댑터 직참조)
            result = adapter.execute(item_id, "AI", "AI_INFERENCE_REQUEST", payload)
            logger.info(
                "UploadInspectionImage AI dispatch result: success=%s message=%s",
                result.success,
                result.message,
            )
        except Exception as exc:  # noqa: BLE001 — handler 격리
            logger.warning(
                "UploadInspectionImage AI dispatch 실패 item_id=%d exc=%s",
                item_id,
                exc,
            )

    threading.Thread(
        target=_run, name=f"ai-dispatch-item-{item_id}", daemon=True
    ).start()


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
        """Jetson Vision Controller 검사 이미지 → backend disk + AI 어댑터 dispatch.

        흐름:
            1. jpeg_bytes 검증 + idempotency dedup (60s TTL)
            2. InspectionImageSinkCommand → MGMT_INSP_IMAGE_SAVE_DIR/<item_id>/<ts>.jpg 저장
            3. AI 어댑터 비동기 dispatch → AI 서버 forward → DB 4-table 갱신 →
               INSP_COMPLETED publish → Jetson 컨베이어 재가동
            4. InspectionImageAck 즉시 반환 (AI 처리는 background)

        env:
            MGMT_INSP_IMAGE_SAVE_DIR  저장 루트 (기본 /var/lib/casting/inspections)
            MGMT_INSP_IMAGE_MAX_FILES item_id 폴더당 최대 보관 수 (기본 20)
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

        captured_at = time.time()
        saved = _image_sink.save(
            item_id=item_id,
            image_bytes=request.jpeg_bytes,
            captured_at=captured_at,
            label=stage,
        )
        if saved is None:
            logger.error(
                "UploadInspectionImage: sink.save 실패 item=%s camera=%s stage=%s",
                item_id, camera_id, stage,
            )
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details("disk save failed")
            return management_pb2.InspectionImageAck(
                accepted=False, stored_path="", reason="disk save failed",
            )

        stored_path = str(saved.path)
        _insp_dedup_record(idem, stored_path)
        logger.info(
            "UploadInspectionImage: saved item=%s camera=%s stage=%s size=%dB path=%s",
            item_id, camera_id, stage, saved.size_bytes, stored_path,
        )

        # AI 사슬 트리거 (background) — disk 저장 성공 시에만 dispatch
        if item_id > 0:
            _dispatch_ai_inference(
                item_id=item_id,
                image_path=stored_path,
                captured_at=captured_at,
                label=stage,
            )
        else:
            logger.info(
                "UploadInspectionImage: item_id=0 — AI dispatch skip (테스트/스모크)",
            )

        return management_pb2.InspectionImageAck(
            accepted=True, stored_path=stored_path, reason="ok",
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
