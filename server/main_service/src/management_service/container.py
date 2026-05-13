import logging

from services.legacy.execution_monitor import ExecutionMonitor
from services.core.orchestrator import Orchestrator
from services.core.task_manager import TaskManager
from services.core.task_allocator import TaskAllocator
from services.core.task_executor import TaskExecutor
from services.core.adapter_router import AdapterRouter
from services.core.traffic_manager import TrafficManager
from services.core.mock_state_manager import MockStateManager
from services.core.event_bridge import EventBridge
from services.core.adapters.ros2_runtime import Ros2Runtime
from services.core.adapters.amr_state_monitor import AmrStateMonitorService
from services.http_image_server import HttpImageServer

from services.query.item_query_service import ItemQueryService
from services.query.pattern_query_service import PatternQueryService
from services.query.production_order_query_service import ProductionOrderQueryService
from services.query.schedule_query_service import ScheduleQueryService
from services.command.pattern_command_service import PatternCommandService

from datetime import datetime, timezone
from services.contracts.enums import EventType
from services.legacy.command_queue import ConveyorCmd, queue as command_queue


logger = logging.getLogger(__name__)

class Container:
    """Dependency Injection Container

    모든 Adapter와 Core 비즈니스 로직을 여기서 생성하고 서로 주입(연결)합니다.
    server.py 에서는 이 container 인스턴스만 import 해서 사용합니다.
    """
    def __init__(self):
        logger.info("Initializing Dependency Container...")
        self._started = False

        self.event_bridge = EventBridge()
        self.state_manager = MockStateManager(event_bridge=self.event_bridge, enable_persistence=True)
        self.task_manager = TaskManager(sm=self.state_manager)
        self.task_allocator = TaskAllocator(state_manager=self.state_manager)
        self.ros2_runtime = Ros2Runtime()
        self.adapter = AdapterRouter(
            ros2_runtime=self.ros2_runtime,
            event_bridge=self.event_bridge,
        )
        self.task_executor = TaskExecutor(
            adapter=self.adapter,
            state_manager=self.state_manager,
            event_bridge=self.event_bridge,
        )
        self.orchestrator = Orchestrator(
            event_bridge=self.event_bridge,
            task_manager=self.task_manager,
            task_allocator=self.task_allocator,
            state_manager=self.state_manager,
            task_executor=self.task_executor,
        )
        self.traffic_manager = TrafficManager()

        self.item_query_service = ItemQueryService()
        self.pattern_query_service = PatternQueryService()
        self.production_order_query_service = ProductionOrderQueryService()
        self.schedule_query_service = ScheduleQueryService()
        self.pattern_command_service = PatternCommandService()
        
        self.execution_monitor = ExecutionMonitor()

        # adapters
        self.amr_state_monitor = AmrStateMonitorService(state_manager=self.state_manager)
        self.amr_battery = self.amr_state_monitor

        # AI 서버 ↔ backend 사이 이미지 URL 전송용 정적 HTTP 서버
        # AI 서버가 /inspect 요청 시 받은 image_url 로 GET 한다.
        self.http_image_server = HttpImageServer()

        # PyQt ③ 후처리 완료 → 1회차 컨베이어 motor RUN 발신
        # (사이클 후반 INSP_COMPLETED → 2회차 4초 RUN 은 ConvAdapter 가 별도 발신)
        self._register_pp_done_motor_run()

        # PyQt ② RFID 스캔 버튼 → ITEM_LOOKUP_REQUESTED → 본 핸들러 → ITEM_LOOKUP_RESULT
        # publish (PyQt 가 WatchEvents 로 받아 item 정보 + 필요옵션 화면 갱신).
        self._register_item_lookup_handler()

        # PR #8 EventBridge publish 단일 채널 정합 — ESP32/Jetson 의 HANDOFF_ACK,
        # RFID_SCANNED, TOF1_ENTRY 이벤트를 EventGateway 로 수신 → backend DB 변경.
        self._register_handoff_ack_responder()
        self._register_rfid_scanned_responder()
        self._register_tof1_entry_responder()

        # INSP_IMAGE_UPLOADED → state_manager 검사 이미지 registry 저장.
        # orchestrator._build_execute_payload(INSP) 가 본 정보를 consume 하여
        # AIAdapter 에게 image_path 를 전달. AIAdapter 직접 호출 fallback 은 P2 에서 제거됨.
        self._register_insp_image_responder()

    def _register_insp_image_responder(self) -> None:
        """INSP_IMAGE_UPLOADED → state_manager.update_inspection_image (registry 저장).

        흐름:
          Jetson UploadInspectionImage RPC → backend disk 저장 + INSP_IMAGE_UPLOADED publish
          → 본 핸들러 → state_manager.update_inspection_image(item_id, payload)
          → orchestrator._build_execute_payload(INSP) 가 consume 후 INSP task payload 에 병합
          → task_executor INSP task step 1 ("AI_INFERENCE_REQUEST") → AdapterRouter → AIAdapter

        2026-05-13 (P2): insp_task_txn INSERT + AIAdapter 직접 호출 fallback 을 제거하고
        정상 task_executor INSP task dispatch 경로로 일원화. AIAdapter 는 추론 결과만
        반환하고, DB 4-table 갱신은 task_executor 가 state_manager.record_inspection_result
        로 위임 (P2.4 완료).
        """
        def _on_insp_image_uploaded(event) -> None:
            payload = event.payload or {}
            try:
                item_id = int(event.item_id) if event.item_id else int(payload.get("item_id") or 0)
            except (TypeError, ValueError):
                item_id = 0
            if item_id <= 0:
                logger.info("[container] INSP_IMAGE_UPLOADED item_id 없음 — skip")
                return

            image_path = (payload.get("image_path") or "").strip()
            if not image_path:
                logger.info(
                    "[container] INSP_IMAGE_UPLOADED image_path 없음 item_id=%s — skip",
                    item_id,
                )
                return

            try:
                captured_at = float(payload.get("captured_at") or datetime.now(timezone.utc).timestamp())
            except (TypeError, ValueError):
                captured_at = datetime.now(timezone.utc).timestamp()

            update = getattr(self.state_manager, "update_inspection_image", None)
            if update is None:
                logger.warning(
                    "[container] state_manager 가 update_inspection_image 미지원 — "
                    "item_id=%s image_path=%s skip",
                    item_id, image_path,
                )
                return

            update(
                item_id,
                {
                    "image_path": image_path,
                    "captured_at": captured_at,
                    "label": payload.get("label") or payload.get("stage") or "INSP",
                    "stage": payload.get("stage") or "INSP",
                    "camera_id": payload.get("camera_id"),
                },
            )
            logger.info(
                "[container] INSP_IMAGE_UPLOADED → state_manager.update_inspection_image "
                "item_id=%s path=%s",
                item_id, image_path,
            )

        self.event_bridge.subscribe(
            EventType.INSP_IMAGE_UPLOADED,
            _on_insp_image_uploaded,
            "container.insp_image_responder",
        )

    def _register_handoff_ack_responder(self) -> None:
        """HANDOFF_ACK → handoff_pipeline.apply_handoff 호출 (item.cur_stat='PP' + pp_task_txn INSERT).

        ESP32 GPIO33 푸시 버튼 → Jetson EventGateway PublishEvent(HANDOFF_ACK) → 본 핸들러.
        idempotency_key 는 esp_bridge 의 `{source_device}:{event_ts_millis}` 형식.
        event_gateway_servicer._envelope_to_event 가 payload._idempotency_key 로 보존.
        HandoffAck row 의 unique idempotency_key 인덱스로 V1 ReportHandoffAck RPC 와
        이중 처리되어도 두 번째는 dedup skip.
        """
        def _on_handoff_ack(event) -> None:
            payload = event.payload or {}
            source_device = (payload.get("source_device")
                             or payload.get("button")
                             or "unknown")
            idem = payload.get("_idempotency_key") or payload.get("idempotency_key") or None

            try:
                from services.legacy.handoff_pipeline import apply_handoff
                from smart_cast_db.database import SessionLocal
                from smart_cast_db.models.models_legacy import HandoffAck
            except Exception as exc:  # noqa: BLE001
                logger.warning("[container] HANDOFF_ACK imports 실패 — skip: %s", exc)
                return

            if idem:
                dup_db = SessionLocal()
                try:
                    dup = (
                        dup_db.query(HandoffAck)
                        .filter(HandoffAck.idempotency_key == idem)
                        .first()
                    )
                    if dup is not None:
                        logger.info(
                            "[container] HANDOFF_ACK dedup hit idem=%s — skip", idem
                        )
                        return
                finally:
                    dup_db.close()

            db = SessionLocal()
            try:
                result = apply_handoff(
                    db,
                    button_device_id=source_device,
                    ack_source="esp32_button",
                    via="event_gateway",
                    idempotency_key=idem,
                )
                db.commit()
                logger.info(
                    "[container] HANDOFF_ACK → apply_handoff released=%s "
                    "item_id=%s amr=%s pp_txns=%s reason=%s",
                    result.released, result.item_id, result.amr_id,
                    result.pp_task_txn_ids, result.reason,
                )
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                logger.warning(
                    "[container] HANDOFF_ACK apply_handoff 예외: %s", exc, exc_info=True
                )
            finally:
                db.close()

        self.event_bridge.subscribe(
            EventType.HANDOFF_ACK,
            _on_handoff_ack,
            "container.handoff_ack_responder",
        )

    def _register_rfid_scanned_responder(self) -> None:
        """RFID_SCANNED → smartcast.log_action_operator_rfid_scan INSERT (RDS 영속화).

        ESP32 RC522 RFID 칩 스캔 → Jetson EventGateway PublishEvent(RFID_SCANNED) → 본 핸들러.
        PyQt 측은 EventGateway WatchEvents 로 직접 받아 _payload_edit 자동 채움 (별개 path).
        본 핸들러는 RDS log INSERT 만 담당 — item lookup / 화면 응답은
        PyQt ② RFID 스캔 버튼 → ITEM_LOOKUP_REQUESTED → item_lookup_responder 가 처리.

        idempotency_key 의 unique partial index 로 V1 ReportRfidScan (현재 disabled) 와
        이중 처리되어도 두 번째는 IntegrityError 로 skip.
        """
        def _on_rfid_scanned(event) -> None:
            payload = event.payload or {}
            raw_payload = (payload.get("raw_payload") or "").strip()
            if not raw_payload:
                logger.info("[container] RFID_SCANNED missing raw_payload — skip")
                return

            reader_id = (payload.get("reader_id") or "unknown").strip() or "unknown"
            zone = payload.get("zone") or None
            idem = payload.get("_idempotency_key") or payload.get("idempotency_key") or None
            scanned_at_unix = (
                payload.get("scanned_at_unix")
                or payload.get("scanned_at_millis")
                or payload.get("scanned_at")
            )

            try:
                from smart_cast_db.database import SessionLocal
                from smart_cast_db.models import RfidScanLog
            except Exception as exc:  # noqa: BLE001
                logger.warning("[container] RFID_SCANNED imports 실패 — skip: %s", exc)
                return

            # scanned_at_unix 가 ms 단위인지 s 단위인지 자동 판별
            try:
                if isinstance(scanned_at_unix, (int, float)) and scanned_at_unix > 0:
                    secs = float(scanned_at_unix)
                    if secs > 1e11:  # 1e11 초 = 3170 년 → ms 단위 확정
                        secs = secs / 1000.0
                    scanned_at = datetime.fromtimestamp(secs, tz=timezone.utc)
                else:
                    scanned_at = datetime.now(timezone.utc)
            except Exception:  # noqa: BLE001
                scanned_at = datetime.now(timezone.utc)

            db = SessionLocal()
            try:
                row = RfidScanLog(
                    scanned_at=scanned_at,
                    reader_id=reader_id,
                    zone=zone,
                    raw_payload=raw_payload,
                    idempotency_key=idem,
                )
                db.add(row)
                db.commit()
                logger.info(
                    "[container] RFID_SCANNED → RfidScanLog INSERT raw_payload=%s "
                    "reader=%s zone=%s scanned_at=%s",
                    raw_payload, reader_id, zone, scanned_at.isoformat(),
                )
            except Exception as exc:  # noqa: BLE001 — idempotency dedup 시 IntegrityError
                db.rollback()
                logger.info(
                    "[container] RFID_SCANNED dedup or insert skip raw_payload=%s "
                    "reader=%s reason=%s",
                    raw_payload, reader_id, exc,
                )
            finally:
                db.close()

        self.event_bridge.subscribe(
            EventType.RFID_SCANNED,
            _on_rfid_scanned,
            "container.rfid_scanned_responder",
        )

    def _register_tof1_entry_responder(self) -> None:
        """TOF1_ENTRY → handoff_pipeline.apply_tof1 (DB 전이만 수행, ToINSP wake-up 없음).

        ESP32 TOF1 (카메라 앞) 센서 detect → Jetson EventGateway
        PublishEvent(TOF1_ENTRY) → 본 핸들러.
        apply_tof1 의 책임 (handoff_pipeline.py:218):
            - pp_task_txn 모두 SUCC (item_id 의 후처리 작업 완료 처리)
            - item.cur_stat='WAIT_INSP'
            - equip_task_txn ToINSP PROC INSERT
            - equip_stat ON

        2026-05-13 (P1): ToINSP task 종결 신호는 더 이상 본 핸들러가 발행하지 않는다.
        후처리 종료 신호는 PyQt 후처리 완료 버튼 → PP_DONE_REQUESTED 가 담당하고,
        ToINSP task 의 종결은 카메라 밑 도달 후 UploadInspectionImage RPC 가
        publish 하는 INSP_IMAGE_UPLOADED 이벤트가 담당한다. 본 핸들러는 ESP32 가
        여전히 TOF1_ENTRY 를 발행하는 동안의 DB 상태 정합성(idempotent fallback) 만
        보장한다. V1 ReportConveyorEvent RPC handler (field_event_rpc.py:217-271) 도
        동일 작업을 수행하므로 양쪽이 중복 실행되어도 apply_tof1 자체가 멱등.
        TOF1 의미 폐기 후 ESP/Jetson 펌웨어가 TOF1_ENTRY 발행을 멈추면 본 핸들러는
        제거 대상.
        """
        def _on_tof1_entry(event) -> None:
            payload = event.payload or {}
            on = payload.get("on", True)
            if not on:
                # OFF edge 는 무시 — apply_tof1 은 ON edge 에서만 호출
                return

            res_id = event.res_id or payload.get("res_id") or "CONV-01"
            rfid_payload = payload.get("rfid_payload") or None
            item_id_raw = event.item_id or payload.get("item_id")

            try:
                from services.legacy.handoff_pipeline import apply_tof1
                from smart_cast_db.database import SessionLocal
            except Exception as exc:  # noqa: BLE001
                logger.warning("[container] TOF1_ENTRY imports 실패 — skip: %s", exc)
                return

            item_id_int = None
            if item_id_raw is not None:
                try:
                    item_id_int = int(item_id_raw)
                    if item_id_int <= 0:
                        item_id_int = None
                except (TypeError, ValueError):
                    item_id_int = None

            db = SessionLocal()
            try:
                r1 = apply_tof1(
                    db,
                    res_id=res_id,
                    rfid_payload=rfid_payload,
                    item_id=item_id_int,
                )
                db.commit()
                logger.info(
                    "[container] TOF1_ENTRY → apply_tof1 ok=%s item_id=%s "
                    "equip_txn=%s reason=%s",
                    r1.ok, r1.item_id, r1.equip_task_txn_id, r1.reason,
                )
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                logger.warning(
                    "[container] TOF1_ENTRY apply_tof1 예외: %s", exc, exc_info=True
                )
            finally:
                db.close()

        self.event_bridge.subscribe(
            EventType.TOF1_ENTRY,
            _on_tof1_entry,
            "container.tof1_entry_responder",
        )

    def _register_item_lookup_handler(self) -> None:
        """ITEM_LOOKUP_REQUESTED → backend Item lookup + ord_pp_map view → ITEM_LOOKUP_RESULT.

        PyQt ② RFID 스캔 버튼 흐름:
          PyQt EventGateway PublishEvent(ITEM_LOOKUP_REQUESTED, payload.raw_payload)
          → 본 핸들러 → handoff_pipeline._resolve_item_by_payload + build_pp_options_view
          → EventBridge.publish(ITEM_LOOKUP_RESULT, payload={item, pp_options})
          → EventGateway WatchEvents (pyqt-pp-worker 구독) 으로 stream
          → PyQt._handle_item_lookup_result slot 이 화면 갱신.

        task_executor 도 ITEM_LOOKUP_REQUESTED 를 subscribe 하여 ToINSP task step 1
        waiter 깨우지만 (task_executor.py:21, 166), EventBridge handler 격리 호출
        이므로 양쪽이 안전하게 공존 (전자는 ToINSP wait 해제, 본 핸들러는 화면 응답).

        Item ORM (item.py) 의 synonym: flow_stat=cur_stat, item_stat_id=item_id,
        zone_nm=cur_res. PyQt _render_item 의 key (item_id/ord_id/cur_stat/
        equip_task_type/cur_res/is_defective) 와 일관된 dict 형식 유지.
        """
        def _on_item_lookup(event) -> None:
            raw_payload = ((event.payload or {}).get("raw_payload") or "").strip()
            if not raw_payload:
                logger.warning(
                    "[container] ITEM_LOOKUP_REQUESTED missing raw_payload — skip"
                )
                return
            try:
                from services.legacy.handoff_pipeline import (
                    _resolve_item_by_payload,
                    build_pp_options_view,
                )
                from services.contracts.models import Event
                from smart_cast_db.database import SessionLocal
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[container] ITEM_LOOKUP imports 실패 — skip: %s", exc
                )
                return

            db = SessionLocal()
            try:
                item = _resolve_item_by_payload(db, raw_payload)
                if item is None:
                    item_dict = None
                    pp_options: list = []
                    logger.info(
                        "[container] ITEM_LOOKUP raw_payload=%s → 매칭 없음",
                        raw_payload,
                    )
                else:
                    pp_options = build_pp_options_view(db, item)
                    item_dict = {
                        "item_id": int(item.item_id),
                        "ord_id": int(item.ord_id) if item.ord_id else 0,
                        "cur_stat": item.cur_stat or "",
                        "equip_task_type": item.equip_task_type or "",
                        "cur_res": item.cur_res or "",
                        "is_defective": (
                            bool(item.is_defective)
                            if item.is_defective is not None else None
                        ),
                    }
                    logger.info(
                        "[container] ITEM_LOOKUP raw_payload=%s → item_id=%s "
                        "cur_stat=%s pp_options=%d",
                        raw_payload, item.item_id, item.cur_stat, len(pp_options),
                    )
                self.event_bridge.publish(
                    Event(
                        event_type=EventType.ITEM_LOOKUP_RESULT,
                        item_id=item.item_id if item is not None else None,
                        payload={
                            "item": item_dict,
                            "pp_options": pp_options,
                            "raw_payload": raw_payload,
                        },
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[container] ITEM_LOOKUP 처리 예외: %s", exc, exc_info=True
                )
            finally:
                db.close()

        self.event_bridge.subscribe(
            EventType.ITEM_LOOKUP_REQUESTED,
            _on_item_lookup,
            "container.item_lookup_responder",
        )

    def _register_pp_done_motor_run(self) -> None:
        """PP_DONE_REQUESTED → 컨베이어 1회차 motor RUN 발신.

        흐름:
            PyQt ③ 후처리 완료 버튼
              → EventGateway PublishEvent(PP_DONE_REQUESTED)
              → 본 핸들러 → command_queue.enqueue("start", "CONV-01")
              → backend WatchConveyorCommands stream (hardware_rpc.py:151)
              → Jetson CommandSubscriber (command_subscriber.py:164)
              → EspBridge.send_command("start") (펌웨어 IDLE → RUNNING)
              → motor ON → 주물 이동 → 카메라 밑 정지 (펌웨어 STOPPED) → Jetson 캡처
              → UploadInspectionImage → INSP_IMAGE_UPLOADED publish
              → task_executor ToINSP waiter 해제 → INSP task → AI/DB chain.

        2회차 motor RUN (검사 완료 후 4초) 은 ConvAdapter.CONV_ALLOW_MOVE
        → INSP_COMPLETED publish → esp_bridge._on_inspection_done 경로 (기존)
        로 별도 처리 — 본 핸들러와 무관.

        task_executor._on_external_wait_event 도 같은 EventType 을 subscribe 하지만
        EventBridge 는 handler 별로 격리 호출하므로 양쪽이 안전하게 공존
        (전자는 PP task waiter 깨움, 본 핸들러는 ESP32 dispatch).
        """
        def _on_pp_done(event) -> None:
            item_id = event.item_id or int((event.payload or {}).get("item_id") or 0)
            command_queue.enqueue(
                ConveyorCmd(
                    robot_id="CONV-01",
                    command="start",
                    item_id=item_id,
                    issued_at_iso=datetime.now(timezone.utc).isoformat(),
                    issued_by="container.pp_done_motor_run",
                )
            )
            logger.info(
                "[container] PP_DONE_REQUESTED → ConveyorCmd(start) enqueued "
                "item_id=%s (1회차 motor RUN)",
                item_id,
            )

        self.event_bridge.subscribe(
            EventType.PP_DONE_REQUESTED,
            _on_pp_done,
            "container.pp_done_motor_run",
        )

    def start(self) -> None:
        """서버 시작 시 adapter들을 실행한다."""
        if self._started:
            return
        logger.info("Starting Dependency Container resources...")
        self.ros2_runtime.start()  # ros2 multi thread 시작
        self.adapter.start()
        self.amr_state_monitor.start()
        self.http_image_server.start()
        self._started = True

    def close(self) -> None:
        """서버 종료 시 실행 중인 adapter들을 정리한다."""
        if not self._started:
            return
        logger.info("Stopping Dependency Container resources...")
        try:
            self.http_image_server.stop()
        finally:
            try:
                self.amr_state_monitor.stop()
            finally:
                try:
                    self.adapter.close()
                finally:
                    self.ros2_runtime.shutdown()
                    self._started = False

# Singleton Container Instance
container = Container()
