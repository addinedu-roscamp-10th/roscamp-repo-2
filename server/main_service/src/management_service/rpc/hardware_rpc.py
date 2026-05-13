"""Hardware streaming RPC methods and image publisher servicer.

이 파일은 PR #9 (UploadInspectionImage RPC + dedup + disk save) 와
본 PR (AI inspection chain wiring) 의 통합 형태이다. PR #9 가 먼저 dev 에
머지될 경우 본 파일과 conflict 가 발생하며, conflict 해결 시 "ours"
(InspectionImageSinkCommand + INSP_IMAGE_UPLOADED publish 포함된 본 버전)
을 채택하면 e2e 사슬이 자동 트리거된다.

2026-05-13 (P0-3): UploadInspectionImage 가 디스크 저장 완료 후 직접 AIAdapter 를
호출하던 백그라운드 dispatch 를 제거하고, EventBridge.publish(INSP_IMAGE_UPLOADED)
단일 채널로 일원화. task_executor 의 ToINSP task 가 본 이벤트를 기다려 종결되고,
이후 INSP task 가 AI 추론을 담당한다 (현재는 container.insp_image_responder
fallback 이 동일 이벤트를 받아 직접 AIAdapter 호출 — orchestrator dispatch 복구
시 fallback 은 제거 예정).
"""

from __future__ import annotations

import logging
import threading
import time

import grpc
import management_pb2  # type: ignore
import management_pb2_grpc  # type: ignore

from services.command.inspection_image_sink_command import InspectionImageSinkCommand
from services.contracts.enums import EventType
from services.contracts.models import Event
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


def _publish_insp_image_uploaded(
    *,
    item_id: int,
    image_path: str,
    captured_at: float,
    stage: str,
    camera_id: str,
    idempotency_key: str,
) -> None:
    """EventBridge.publish(INSP_IMAGE_UPLOADED) — ToINSP task 종결 + INSP 사슬 시작 trigger.

    구독자:
      - task_executor: ToINSP task step 1 (WAIT_SUBTASK_COMPLETED) waiter 해제
        → ToINSP task SUCC → TaskManager 가 INSP task 생성
        → INSP task step 1 (AI_INFERENCE_REQUEST) 실행 → AIAdapter
      - container.insp_image_responder (fallback): orchestrator dispatch 복구 전까지
        직접 insp_task_txn INSERT + AIAdapter.execute() 호출

    AIAdapter 는 EventBridge 를 사용하지 않으며, 추론 결과는 AdapterResult 로만
    반환되어 task_executor 가 StateManager 에게 위임 (목표 상태).

    publish 실패는 warning 로깅만 — INSP 흐름 누락은 다음 cycle 까지 추적 필요.
    """
    try:
        # 지연 import — container 초기화 순서 의존성
        from container import container
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "UploadInspectionImage: container import 실패 — INSP_IMAGE_UPLOADED publish skip (%s)",
            exc,
        )
        return

    try:
        container.event_bridge.publish(
            Event(
                event_type=EventType.INSP_IMAGE_UPLOADED,
                item_id=item_id,
                payload={
                    "item_id": item_id,
                    "image_path": image_path,
                    "captured_at": captured_at,
                    "stage": stage,
                    "camera_id": camera_id,
                    "label": stage,
                    "_idempotency_key": idempotency_key,
                },
            )
        )
        logger.info(
            "UploadInspectionImage → INSP_IMAGE_UPLOADED publish item_id=%d path=%s",
            item_id,
            image_path,
        )
    except Exception as exc:  # noqa: BLE001 — handler 격리
        logger.warning(
            "UploadInspectionImage: INSP_IMAGE_UPLOADED publish 실패 item_id=%d exc=%s",
            item_id,
            exc,
        )


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
        """Jetson Vision Controller 검사 이미지 → backend disk + INSP_IMAGE_UPLOADED publish.

        흐름:
            1. jpeg_bytes 검증 + idempotency dedup (60s TTL)
            2. InspectionImageSinkCommand → MGMT_INSP_IMAGE_SAVE_DIR/<item_id>/<ts>.jpg 저장
            3. EventBridge.publish(INSP_IMAGE_UPLOADED, payload={image_path, ...})
               → task_executor ToINSP task waiter 해제 → INSP task → AIAdapter
               → AI 결과 반환 → StateManager 가 DB 4-table 갱신
               → INSP_COMPLETED publish (conv_adapter ToPAWait/CONV_ALLOW_MOVE) → 컨베이어 재가동
            4. InspectionImageAck 즉시 반환

        AIAdapter 직접 호출은 본 핸들러에서 제거됨 (2026-05-13). EventBridge 단일 채널로
        일원화되어 task_executor 의 INSP task 가 추론 흐름의 owner.

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

        # ToINSP task 종결 + INSP 사슬 시작 trigger — EventBridge 단일 채널 publish.
        # disk 저장 성공 시에만 publish (item_id<=0 인 테스트 스모크 호출은 skip).
        if item_id > 0:
            _publish_insp_image_uploaded(
                item_id=item_id,
                image_path=stored_path,
                captured_at=captured_at,
                stage=stage,
                camera_id=camera_id,
                idempotency_key=idem,
            )
        else:
            logger.info(
                "UploadInspectionImage: item_id=0 — INSP_IMAGE_UPLOADED publish skip (테스트/스모크)",
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
