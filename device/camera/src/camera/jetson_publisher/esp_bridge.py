"""ESP32 Serial Bridge — Jetson ↔ Conveyor 펌웨어(v5) 이벤트 중계.

V6 아키텍처 (2026-04-15 업데이트 · 2026-04-17 SPEC-AMR-001 추가):
    [ESP32 conveyor v1.4.0] --USB Serial 115200 ASCII-- [Jetson publisher]

본 모듈은 publisher.py 와 **같은 프로세스** 내 별도 스레드로 실행.
- RX(ESP→Jetson): STATE/STOPPED/STARTED/DONE/PONG/SENSOR* / HANDOFF_ACK
- TX(Jetson→ESP): RUN/STOP/PING/STATUS 등 명령 송신
- 핵심 이벤트:
    · "STOPPED" 수신 → (지연 INSPECTION_DELAY_MS) → "RUN\\n" 송신
        - 지연 동안 publisher 가 적어도 1 프레임을 서버로 push 완료한다는 가정
        - 2 fps 기준 기본 500 ms + 여유 → 기본 700 ms
    · "HANDOFF_ACK" 수신 (SPEC-AMR-001) → Management Service ReportHandoffAck gRPC
        - 메모리 버퍼 32 이벤트 + 지수 백오프 1s→60s 재시도
        - idempotency_key = "source_device:ts_millis" 로 중복 방지
- 재연결: Serial 끊기면 10s backoff

환경변수:
    ESP_BRIDGE_ENABLED           기본 0 (opt-in). 1 이면 스레드 기동
    ESP_BRIDGE_PORT              기본 /dev/ttyUSB0
    ESP_BRIDGE_BAUD              기본 115200
    ESP_BRIDGE_AUTO_RUN          기본 1 (STOPPED 시 자동 RUN 송신). 0 이면 로그만
    ESP_BRIDGE_INSPECTION_DELAY_MS  기본 700 (STOPPED 후 RUN 까지 대기)
    ESP_BRIDGE_DEVICE_ID         기본 ESP-CONVEYOR-01 (HandoffAck source_device)
    MANAGEMENT_GRPC_TARGET       기본 localhost:50051 (gRPC ReportHandoffAck 전송지)
    MANAGEMENT_GRPC_DISABLED     기본 0. 1 이면 gRPC 스텁 없이도 로그만

@MX:NOTE STOPPED 순간의 "정확한 한 프레임" 보장이 필요하면 publisher 와 이벤트 동기
        메커니즘(threading.Event) 을 추가하세요. 현재 MVP 는 시간 기반 지연으로 단순화.
@MX:WARN publisher 가 /dev/ttyUSB0 을 쓰지 않음 (카메라는 /dev/video0). 충돌 없음.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass

log = logging.getLogger("jetson.esp_bridge")


# SPEC-AMR-001: 버퍼링 큐 용량 / 지수 백오프 상한
HANDOFF_BUFFER_MAX = 32
HANDOFF_BACKOFF_START_S = 1.0
HANDOFF_BACKOFF_CAP_S = 60.0


@dataclass
class PendingHandoff:
    """gRPC 전송 대기 중인 HandoffAck 이벤트."""

    source_device: str
    zone: str
    occurred_at_millis: int  # ESP32 millis() (reboot 시 리셋되지만 idempotency 용도)
    idempotency_key: str


@dataclass
class PendingConveyorEvent:
    """gRPC 전송 대기 중인 ConveyorEvent (TOF1/TOF2)."""

    res_id: str
    event_type: str  # "tof1_entry" | "tof2_exit"
    occurred_at_millis: int
    idempotency_key: str


@dataclass
class PendingRfidScan:
    """gRPC 전송 대기 중인 ReportRfidScan 이벤트.

    SPEC-RFID-001 (2026-05-07): ESP32 RC522 가 NDEF Text 레코드를 스캔하면
    JSON 라인으로 출력 → esp_bridge 가 파싱 후 Mgmt ReportRfidScan RPC 로 push.
    """

    reader_id: str
    zone: str
    raw_payload: str  # NDEF Text 레코드 본문 (예: "order_1_item_20260417_1")
    occurred_at_millis: int
    idempotency_key: str


class EspBridge:
    """ESP32 serial 브리지 스레드."""

    def __init__(
        self,
        shutdown_event: threading.Event,
        port: str = "/dev/ttyUSB0",
        baud: int = 115200,
        auto_run: bool = True,
        inspection_delay_ms: int = 700,
        reconnect_sec: float = 10.0,
        device_id: str = "ESP-CONVEYOR-01",
        grpc_target: str = "localhost:50051",
        grpc_disabled: bool = False,
        rfid_reader_id: str = "RFID-CONV-01",
        rfid_zone: str = "CONV-IN",
    ) -> None:
        self._shutdown = shutdown_event
        self._port = port
        self._baud = baud
        self._auto_run = auto_run
        self._inspection_delay = inspection_delay_ms / 1000.0
        self._reconnect = reconnect_sec
        self._device_id = device_id
        self._grpc_target = grpc_target
        self._grpc_disabled = grpc_disabled
        self._handoff_queue: deque[PendingHandoff] = deque(maxlen=HANDOFF_BUFFER_MAX)
        self._handoff_lock = threading.Lock()
        # 2026-04-26: 컨베이어 TOF 이벤트 큐 (handoff 와 동일 패턴)
        self._conveyor_queue: deque[PendingConveyorEvent] = deque(maxlen=HANDOFF_BUFFER_MAX)
        self._conveyor_lock = threading.Lock()
        # 환경변수 ESP_BRIDGE_CONVEYOR_RES_ID (default CONV-01) — STATE → res_id 매핑
        self._conveyor_res_id = os.environ.get("ESP_BRIDGE_CONVEYOR_RES_ID", "CONV-01")
        # 2026-05-07 (SPEC-RFID-001): RFID 스캔 이벤트 큐 (handoff/conveyor 와 동일 패턴)
        self._rfid_queue: deque[PendingRfidScan] = deque(maxlen=HANDOFF_BUFFER_MAX)
        self._rfid_lock = threading.Lock()
        self._rfid_reader_id = rfid_reader_id
        self._rfid_zone = rfid_zone
        # production firmware (conveyor_v5_serial) 가 매 ~335ms 마다 status snapshot 을
        # 출력하므로 edge detection — 이전과 다른 uid 감지 시에만 enqueue.
        self._last_rfid_uid: str | None = None
        # 2026-05-08: STOPPED 캡처 파일명에 사용할 마지막 NDEF text 보존.
        # _consume_last_rfid_label() 가 1회 사용 후 clear (stale 방지).
        self._last_rfid_text: str | None = None
        # 1.7.0 (2026-05-08): TOF1 (카메라 앞 정지 센서) edge push — PyQt indicator 실시간 표시.
        # status snapshot 의 tof1.det 값이 변경될 때마다 backend HTTP POST.
        # MGMT_HTTP_TARGET 환경변수 미설정 시 push 비활성화 (gracefully no-op).
        self._last_tof1_det: bool | None = None
        self._mgmt_http_target = os.environ.get("MGMT_HTTP_TARGET", "").strip()
        # 임시 HTTP 세션 — requests.Session 으로 connection pooling
        self._http_session = None  # lazy import 회피
        # Phase D-2 (V6 canonical): Management → Jetson → ESP32 명령 relay 큐.
        # CommandSubscriber 가 send_command(cmd) 로 enqueue 하면 _session 루프가 Serial 로 drain.
        self._tx_queue: deque[str] = deque()
        self._tx_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="esp-bridge", daemon=True)
        self._handoff_thread = threading.Thread(
            target=self._handoff_drain_loop, name="esp-handoff-drain", daemon=True
        )
        self._conveyor_thread = threading.Thread(
            target=self._conveyor_drain_loop, name="esp-conveyor-drain", daemon=True
        )
        self._rfid_thread = threading.Thread(
            target=self._rfid_drain_loop, name="esp-rfid-drain", daemon=True
        )

    @classmethod
    def from_env(cls, shutdown_event: threading.Event) -> EspBridge:
        return cls(
            shutdown_event=shutdown_event,
            port=os.environ.get("ESP_BRIDGE_PORT", "/dev/ttyUSB0"),
            baud=int(os.environ.get("ESP_BRIDGE_BAUD", "115200")),
            auto_run=os.environ.get("ESP_BRIDGE_AUTO_RUN", "1") in ("1", "true", "yes"),
            inspection_delay_ms=int(os.environ.get("ESP_BRIDGE_INSPECTION_DELAY_MS", "700")),
            device_id=os.environ.get("ESP_BRIDGE_DEVICE_ID", "ESP-CONVEYOR-01"),
            grpc_target=os.environ.get("MANAGEMENT_GRPC_TARGET", "localhost:50051"),
            grpc_disabled=os.environ.get("MANAGEMENT_GRPC_DISABLED", "0") in ("1", "true", "yes"),
            rfid_reader_id=os.environ.get("ESP_BRIDGE_RFID_READER_ID", "RFID-CONV-01"),
            rfid_zone=os.environ.get("ESP_BRIDGE_RFID_ZONE", "CONV-IN"),
        )

    def start(self) -> None:
        if self._thread.is_alive():
            return
        log.info(
            "EspBridge 시작: port=%s baud=%d auto_run=%s delay=%.2fs device=%s grpc=%s rfid=%s/%s",
            self._port,
            self._baud,
            self._auto_run,
            self._inspection_delay,
            self._device_id,
            "disabled" if self._grpc_disabled else self._grpc_target,
            self._rfid_reader_id,
            self._rfid_zone,
        )
        self._thread.start()
        self._handoff_thread.start()
        self._conveyor_thread.start()
        self._rfid_thread.start()
        # 2026-05-14: EventGateway INSP_COMPLETED 구독 경로 제거. 컨베이어 모터
        # 명령은 모두 WatchConveyorCommands 채널 (CommandSubscriber) 로 일원화 —
        # PP_DONE 1 차 "start" 와 ToPAWait/CONV_ALLOW_MOVE 2 차 "RUN" 둘 다 동일 경로.

    # ---------- internal ----------
    def _run(self) -> None:
        try:
            import serial  # lazy
        except ImportError:
            log.error("pyserial 미설치. `pip install pyserial` 필요")
            return

        while not self._shutdown.is_set():
            ser = None
            try:
                ser = serial.Serial(self._port, self._baud, timeout=0.5)
                # ESP32 DTR 토글에 의한 reset 대기 + 부팅 라인 버림
                time.sleep(1.5)
                ser.reset_input_buffer()
                log.info("EspBridge connected to %s @ %d", self._port, self._baud)
                self._session(ser)
            except Exception as e:  # noqa: BLE001
                if self._shutdown.is_set():
                    break
                log.warning("EspBridge serial 오류: %s (재연결 %.1fs)", e, self._reconnect)
                self._shutdown.wait(self._reconnect)
            finally:
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:  # noqa: BLE001
                        pass
        log.info("EspBridge stopped")

    def _session(self, ser) -> None:
        """개방된 serial 세션에서 라인 단위 이벤트 처리 + outbound 큐 drain."""
        buf = bytearray()
        while not self._shutdown.is_set():
            chunk = ser.read(64)
            if chunk:
                buf += chunk
                while b"\n" in buf:
                    line_b, _, rest = buf.partition(b"\n")
                    buf = bytearray(rest)
                    line = line_b.decode(errors="replace").strip()
                    if line:
                        self._handle_line(ser, line)
            # Phase D-2: Management 에서 수신한 명령을 Serial 로 relay
            self._drain_tx(ser)

    # ---------- Outbound command relay (Phase D-2: Management → ESP32) ----------
    def send_command(self, cmd: str) -> None:
        """외부 스레드(CommandSubscriber)가 호출. Serial 접근은 _session 루프가 drain.

        cmd 끝에 newline 없으면 자동 추가. 큰 payload 는 분할 전송하지 않음 (ESP32 버퍼 한계 주의).
        """
        if not cmd:
            return
        normalized = cmd if cmd.endswith("\n") else cmd + "\n"
        with self._tx_lock:
            self._tx_queue.append(normalized)

    def _drain_tx(self, ser) -> None:
        """큐의 모든 outbound 명령을 Serial 로 flush. 스레드-안전."""
        with self._tx_lock:
            if not self._tx_queue:
                return
            pending = list(self._tx_queue)
            self._tx_queue.clear()
        for line in pending:
            try:
                ser.write(line.encode("utf-8"))
                ser.flush()
                log.info("Jetson → ESP32 cmd: %s", line.strip())
            except Exception as e:  # noqa: BLE001
                log.warning("outbound cmd 송신 실패: %s (%s)", line.strip(), e)

    def _handle_line(self, ser, line: str) -> None:
        # 2026-05-07 (SPEC-RFID-001): JSON 이벤트 분기 — RC522 RFID 펌웨어 출력
        # 예: {"event":"rfid_tag","uid":"AA:BB:CC:DD","type":"NTAG213","text":"order_1_item_20260417_1"}
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                log.debug("ESP non-JSON line skipped: %s", line[:80])
            else:
                self._handle_json_event(obj)
                return

        if line == "STOPPED":
            log.info("ESP → STOPPED (inspection trigger)")
            # 2026-05-06: Management 응답 없이도 디스크 저장이 필요한 검증 모드 지원.
            # MGMT_CAM_INSP_SAVE_DIR 가 설정되어 있으면 STOPPED 시점에 즉시 캡처해
            # capture_inspection 의 디스크 저장 hook 을 트리거한다.
            if os.environ.get("MGMT_CAM_INSP_SAVE_DIR", "").strip():
                # 모터/주물 정지 후 진동 안정 + 카메라 AE/AWB 안정화 시간.
                # MGMT_CAM_INSP_STABILIZE_MS env 로 조절 (기본 500ms).
                try:
                    stabilize_ms = int(
                        os.environ.get("MGMT_CAM_INSP_STABILIZE_MS", "500")
                    )
                except ValueError:
                    stabilize_ms = 500
                if stabilize_ms > 0:
                    time.sleep(stabilize_ms / 1000.0)
                try:
                    from capture_inspection import capture_jpeg as _capture
                    rfid_label = self._consume_last_rfid_label()
                    cap_result = _capture(rfid_label=rfid_label)
                    log.info(
                        "[stopped_capture] ok=%s reason=%s size=%dx%d label=%s",
                        cap_result.ok,
                        cap_result.reason or "-",
                        cap_result.width,
                        cap_result.height,
                        rfid_label or "-",
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("[stopped_capture] capture_jpeg() failed: %s", e)
            if self._auto_run:
                # publisher 가 현재 프레임을 서버로 push 할 시간 부여
                time.sleep(self._inspection_delay)
                try:
                    ser.write(b"RUN\n")
                    ser.flush()
                    log.info("Jetson → RUN (after %.2fs delay)", self._inspection_delay)
                except Exception as e:  # noqa: BLE001
                    log.warning("RUN 송신 실패: %s", e)
            else:
                log.info("auto_run=0, STOPPED 대기만 (수동 RUN 필요)")
        elif line == "STARTED":
            log.info("ESP → STARTED (motor on)")
        elif line == "DONE":
            log.info("ESP → DONE (motor off after 4s)")
        elif line.startswith("BOOT:"):
            log.info("ESP bootup: %s", line)
        elif line.startswith("STATE:"):
            # 2026-04-26: STATE:RUNNING (TOF1 entry) / STATE:STOPPED (TOF2 exit) → ConveyorEvent
            state_name = line.split(":", 1)[1].strip()
            event_type: str | None = None
            if state_name == "RUNNING":
                event_type = "tof1_entry"
            elif state_name == "STOPPED":
                event_type = "tof2_exit"

            if event_type is not None:
                now_ms = int(time.time() * 1000)
                pending = PendingConveyorEvent(
                    res_id=self._conveyor_res_id,
                    event_type=event_type,
                    occurred_at_millis=now_ms,
                    idempotency_key=f"{self._conveyor_res_id}:{event_type}:{now_ms}",
                )
                with self._conveyor_lock:
                    if len(self._conveyor_queue) >= HANDOFF_BUFFER_MAX:
                        dropped = self._conveyor_queue.popleft()
                        log.warning(
                            "ConveyorEvent 버퍼 overflow — 가장 오래된 이벤트 폐기 (key=%s)",
                            dropped.idempotency_key,
                        )
                    self._conveyor_queue.append(pending)
                log.info(
                    "ESP → STATE:%s → %s queued (key=%s)",
                    state_name,
                    event_type,
                    pending.idempotency_key,
                )
            else:
                log.debug("ESP state: %s", line)
        elif line.startswith("PONG"):
            log.debug("ESP pong")
        elif line.startswith("ERR:"):
            log.warning("ESP error: %s", line)
        elif line == "HANDOFF_ACK":
            # SPEC-AMR-001: 후처리존 핸드오프 버튼 이벤트 수신.
            # JSON 동반 라인 (ts 포함) 은 JSON 파싱이 비효율 → millis 는 현재 시각 기반으로 대체.
            # ESP32 리부트 시 millis 리셋되지만 idempotency_key 는 source_device + wall_time 로 유일.
            now_ms = int(time.time() * 1000)
            pending = PendingHandoff(
                source_device=self._device_id,
                zone="postprocessing",
                occurred_at_millis=now_ms,
                idempotency_key=f"{self._device_id}:{now_ms}",
            )
            with self._handoff_lock:
                if len(self._handoff_queue) >= HANDOFF_BUFFER_MAX:
                    dropped = self._handoff_queue.popleft()
                    log.warning(
                        "Handoff 버퍼 overflow — 가장 오래된 이벤트 폐기 (key=%s)",
                        dropped.idempotency_key,
                    )
                self._handoff_queue.append(pending)
            log.info(
                "ESP → HANDOFF_ACK (queued key=%s, qsize=%d)",
                pending.idempotency_key,
                len(self._handoff_queue),
            )
            # 2026-05-08 EventGateway channel — sole external wire to backend EventBridge.
            self._publish_via_event_gateway(
                event_type="HANDOFF_ACK",
                resource_id=self._conveyor_res_id,
                payload={
                    "zone": "postprocessing",
                    "source_device": self._device_id,
                    "button": "gpio33",
                },
                idempotency_key=pending.idempotency_key,
            )
        else:
            log.debug("ESP line: %s", line)

    # ---------- JSON event dispatcher (SPEC-RFID-001) ----------
    def _handle_json_event(self, obj: dict) -> None:
        """ESP32 펌웨어가 출력한 JSON 이벤트 분기.

        두 펌웨어 형식 모두 지원:

        1) Debug firmware (conveyor_v5_debug.ino) — event-based:
           {"event":"rfid_tag","uid":"AA:BB:CC:DD","type":"NTAG213","text":"order_1_item_..."}
           {"event":"rfid_clear","uid":"AA:BB:CC:DD"}
           {"event":"rfid_reader","status":"ready",...}

        2) Production firmware (conveyor_v5_serial.ino) — status snapshot (~335ms 폴링):
           {"state":"IDLE","elapsed":...,"motor":false,
            "tof1":{...},"tof2":{...},
            "rfid":{"ready":true,"uid":"...","text":"...","version":"..."},
            "count":1}
        """
        event = (obj.get("event") or "").strip()

        # === Path 1: event-based (debug firmware) ===
        if event == "rfid_tag":
            self._enqueue_rfid_scan(
                uid=(obj.get("uid") or "").strip(),
                text=(obj.get("text") or "").strip(),
                source="event",
            )
            return
        if event == "rfid_clear":
            log.debug("ESP → rfid_clear uid=%s", obj.get("uid"))
            return
        if event == "rfid_reader":
            log.info(
                "ESP RFID reader status=%s spi=%s",
                obj.get("status"),
                obj.get("spi"),
            )
            return
        if event:
            log.debug("ESP JSON event=%s skipped: %s", event, obj)
            return

        # === Path 2: status snapshot (production firmware) ===
        if "state" in obj and isinstance(obj.get("rfid"), dict):
            self._handle_status_snapshot(obj)
            return

        log.debug("ESP JSON skipped (no event/state): %s", obj)

    def _handle_status_snapshot(self, snap: dict) -> None:
        """conveyor_v5_serial production firmware status snapshot 처리.

        매 ~335ms 폴링되므로 edge detection 적용:
          - rfid.uid 가 이전과 다를 때만 RFID 이벤트 enqueue
          - tof1.det 가 이전과 다를 때만 backend 에 sensor state HTTP POST (PyQt 표시용)
        rfid.text 가 비어있으면 NDEF Text 가 굽혀지지 않은 태그 → warning + skip.

        2026-05-13: TOF1_ENTRY EventGateway publish 제거.
        ToINSP task 종결 신호는 backend 가 UploadInspectionImage RPC 도착 후 publish
        하는 INSP_IMAGE_UPLOADED 가 담당한다. TOF1 센서는 PyQt 후처리 완료 버튼
        (PP_DONE_REQUESTED) 으로 의미가 대체되었으므로 본 시점의 publish 는 무의미.
        HTTP POST sensor state (_push_sensor_state) 는 PyQt indicator 실시간 표시
        용도이므로 유지한다.
        """
        # --- TOF1 edge: PyQt indicator HTTP POST 만 발행 (EventGateway publish 제거됨) ---
        tof1 = snap.get("tof1") or {}
        if isinstance(tof1, dict):
            tof1_det = bool(tof1.get("det"))
            if tof1_det != self._last_tof1_det:
                self._last_tof1_det = tof1_det
                raw_mm = tof1.get("mm") if isinstance(tof1.get("mm"), int) else None
                self._push_sensor_state("tof1", tof1_det, raw_mm)

        # --- RFID edge enqueue (기존 SPEC-RFID-001) ---
        rfid = snap.get("rfid") or {}
        uid = (rfid.get("uid") or "").strip()
        text = (rfid.get("text") or "").strip()

        if not uid:
            if self._last_rfid_uid is not None:
                log.debug("ESP RFID cleared (was %s)", self._last_rfid_uid)
                self._last_rfid_uid = None
            # NOTE: _last_rfid_text 는 clear 하지 않는다. 태그 hold 가 짧고
            # STOPPED 가 그 직후 발생하므로, 캡처 파일명에 직전 라벨이
            # 사용될 수 있도록 _consume_last_rfid_label() 까지 유지.
            return

        if uid == self._last_rfid_uid:
            # 같은 태그 status 중복 (펌웨어 hold 동안 반복 출력)
            # — NDEF text 가 늦게 도착하는 경우 보강 갱신
            if text and not self._last_rfid_text:
                self._last_rfid_text = text
            return

        self._last_rfid_uid = uid
        self._last_rfid_text = text or None
        self._enqueue_rfid_scan(uid=uid, text=text, source="status")

    def _consume_last_rfid_label(self) -> str | None:
        """STOPPED 캡처 파일명에 쓸 마지막 NDEF text 를 1회 소비 후 clear.

        다음 cycle 에서 stale 라벨이 재사용되는 것을 방지. RFID 가 다시 스캔되면
        _handle_status_snapshot 에서 새로 채워진다.
        """
        label = self._last_rfid_text
        self._last_rfid_text = None
        return label

    # ---------- TOF1 sensor edge HTTP push (1.7.0) ----------
    def _push_sensor_state(self, sensor_id: str, on: bool, raw_mm: int | None) -> None:
        """Sensor edge 1 회 발생 시 backend 로 HTTP POST 푸시.

        MGMT_HTTP_TARGET (예: "100.77.239.25:8000") 미설정이면 no-op.
        실패는 경고 로깅만 — sensor edge 누락은 다음 edge 에 자동 회복.
        """
        if not self._mgmt_http_target:
            return
        try:
            import requests  # lazy
        except ImportError:
            log.warning("requests 미설치 — sensor state push 비활성")
            return

        if self._http_session is None:
            self._http_session = requests.Session()

        url = (
            f"http://{self._mgmt_http_target}/api/conveyor/"
            f"{self._conveyor_res_id}/sensor/{sensor_id}/state"
        )
        body = {"on": bool(on), "raw_mm": int(raw_mm) if isinstance(raw_mm, int) else None}
        try:
            r = self._http_session.post(url, json=body, timeout=1.5)
            if r.status_code >= 400:
                log.warning(
                    "sensor state push %d: %s body=%s",
                    r.status_code,
                    url,
                    body,
                )
            else:
                log.info(
                    "sensor state push %s.%s = %s (raw_mm=%s)",
                    self._conveyor_res_id,
                    sensor_id,
                    on,
                    raw_mm,
                )
        except Exception as e:  # noqa: BLE001
            log.warning("sensor state push 실패 url=%s body=%s err=%s", url, body, e)

    # ---------- EventGateway publish helper (2026-05-08) ----------
    def _publish_via_event_gateway(
        self,
        *,
        event_type: str,
        resource_id: str = "",
        payload: dict | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        """EventGateway client 활성화 시 publish. 비활성/실패 silent skip.

        Sole external channel to backend EventBridge — 다른 backend service 와의
        직접 통신은 점진적으로 본 channel 로 일원화.
        """
        try:
            from event_gateway_client import get_singleton

            client = get_singleton()
        except Exception:  # noqa: BLE001
            return
        if client is None:
            return
        try:
            client.publish_event(
                event_type=event_type,
                resource_id=resource_id,
                payload=payload or {},
                idempotency_key=idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "event_gateway publish 실패 event_type=%s: %s", event_type, exc,
            )

    def _enqueue_rfid_scan(self, *, uid: str, text: str, source: str) -> None:
        """RFID 태그 1건을 PendingRfidScan 큐에 적재 — 두 펌웨어 형식 공통 path."""
        if not text:
            log.warning(
                "RFID tag detected without NDEF text — uid=%s skip "
                "(태그에 NDEF Text 'order_<ord>_item_<date>_<seq>' 가 굽혀져 있어야 함)",
                uid or "?",
            )
            return

        now_ms = int(time.time() * 1000)
        tag_id = uid or text
        idem_key = f"{self._rfid_reader_id}:{tag_id}:{now_ms}"
        pending = PendingRfidScan(
            reader_id=self._rfid_reader_id,
            zone=self._rfid_zone,
            raw_payload=text,
            occurred_at_millis=now_ms,
            idempotency_key=idem_key,
        )
        with self._rfid_lock:
            if len(self._rfid_queue) >= HANDOFF_BUFFER_MAX:
                dropped = self._rfid_queue.popleft()
                log.warning(
                    "RFID 버퍼 overflow — 가장 오래된 이벤트 폐기 (key=%s)",
                    dropped.idempotency_key,
                )
            self._rfid_queue.append(pending)
        log.info(
            "ESP → rfid[%s] uid=%s text=%s queued (key=%s)",
            source,
            uid or "?",
            text,
            idem_key,
        )
        # 2026-05-08 EventGateway channel — RFID 스캔 사실 publish.
        self._publish_via_event_gateway(
            event_type="RFID_SCANNED",
            resource_id="",  # reader_id 가 길어 payload 에 보존 (length 가드)
            payload={
                "reader_id": self._rfid_reader_id,
                "zone": self._rfid_zone,
                "raw_payload": text,
                "uid": uid,
                "source_kind": source,
            },
            idempotency_key=idem_key,
        )

    # ---------- Handoff ACK drain (SPEC-AMR-001) ----------
    def _handoff_drain_loop(self) -> None:
        """큐에 쌓인 HandoffAck 이벤트를 Management gRPC 로 전송.

        지수 백오프 1s → 60s. gRPC 실패 시 큐 맨 앞 항목 유지, 성공 시 제거.
        """
        backoff = HANDOFF_BACKOFF_START_S
        stub = self._build_grpc_stub()

        while not self._shutdown.is_set():
            with self._handoff_lock:
                if not self._handoff_queue:
                    item: PendingHandoff | None = None
                else:
                    item = self._handoff_queue[0]

            if item is None:
                self._shutdown.wait(0.5)
                continue

            if self._grpc_disabled or stub is None:
                log.info(
                    "Handoff drain: gRPC disabled/unavailable — 큐 유지 (key=%s)",
                    item.idempotency_key,
                )
                self._shutdown.wait(5.0)
                # stub 재시도
                if not self._grpc_disabled:
                    stub = self._build_grpc_stub()
                continue

            try:
                self._send_handoff_ack(stub, item)
                with self._handoff_lock:
                    if self._handoff_queue and self._handoff_queue[0] is item:
                        self._handoff_queue.popleft()
                log.info("Handoff ACK 전송 성공 key=%s", item.idempotency_key)
                backoff = HANDOFF_BACKOFF_START_S  # 성공 시 리셋
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "Handoff ACK 전송 실패 (backoff %.1fs): %s key=%s",
                    backoff,
                    e,
                    item.idempotency_key,
                )
                self._shutdown.wait(backoff)
                backoff = min(backoff * 2, HANDOFF_BACKOFF_CAP_S)
                # stub 재생성 (연결 복구 시도)
                stub = self._build_grpc_stub()

    def _build_grpc_stub(self):
        """Management Service gRPC stub. import/연결 실패 시 None."""
        if self._grpc_disabled:
            return None
        try:
            import grpc  # type: ignore

            # jetson_publisher/generated/ 는 PYTHONPATH 에 이미 있음 (publisher.py 에서 설정)
            from generated import management_pb2_grpc  # type: ignore

            channel = grpc.insecure_channel(self._grpc_target)
            return management_pb2_grpc.ManagementServiceStub(channel)
        except Exception as e:  # noqa: BLE001
            log.warning("gRPC stub 생성 실패: %s", e)
            return None

    def _send_handoff_ack(self, stub, item: PendingHandoff) -> None:
        """gRPC ReportHandoffAck 호출. 실패 시 예외 전파 (drain 루프가 재시도)."""
        from generated import management_pb2  # type: ignore

        # V2 schema: HandoffAckEvent → HandoffAckEvent (메시지명 변경).
        req = management_pb2.HandoffAckEvent(
            source_device=item.source_device,
            zone=item.zone,
            occurred_at=management_pb2.Timestamp(
                iso8601=time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",
                    time.gmtime(item.occurred_at_millis / 1000.0),
                ),
            ),
            idempotency_key=item.idempotency_key,
        )
        resp = stub.ReportHandoffAck(req, timeout=3.0)
        # V2 응답: accepted/reason 만 제공 (V1 의 task_id/amr_id 는 V2 에서 SERVER 측 로깅만).
        log.info(
            "ReportHandoffAck 응답: accepted=%s reason=%s",
            resp.accepted,
            resp.reason,
        )

    # ---------- Conveyor Event drain (TOF1/TOF2 → Mgmt gRPC) ----------
    def _conveyor_drain_loop(self) -> None:
        """큐에 쌓인 ConveyorEvent 를 Management gRPC 로 전송 (handoff 와 동일 패턴)."""
        backoff = HANDOFF_BACKOFF_START_S
        stub = self._build_grpc_stub()

        while not self._shutdown.is_set():
            with self._conveyor_lock:
                if not self._conveyor_queue:
                    item: PendingConveyorEvent | None = None
                else:
                    item = self._conveyor_queue[0]

            if item is None:
                self._shutdown.wait(0.5)
                continue

            if self._grpc_disabled or stub is None:
                log.info(
                    "Conveyor drain: gRPC disabled/unavailable — 큐 유지 (key=%s)",
                    item.idempotency_key,
                )
                self._shutdown.wait(5.0)
                if not self._grpc_disabled:
                    stub = self._build_grpc_stub()
                continue

            try:
                self._send_conveyor_event(stub, item)
                with self._conveyor_lock:
                    if self._conveyor_queue and self._conveyor_queue[0] is item:
                        self._conveyor_queue.popleft()
                log.info("ConveyorEvent 전송 성공 key=%s", item.idempotency_key)
                backoff = HANDOFF_BACKOFF_START_S
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "ConveyorEvent 전송 실패 (backoff %.1fs): %s key=%s",
                    backoff,
                    e,
                    item.idempotency_key,
                )
                self._shutdown.wait(backoff)
                backoff = min(backoff * 2, HANDOFF_BACKOFF_CAP_S)
                stub = self._build_grpc_stub()

    def _send_conveyor_event(self, stub, item: PendingConveyorEvent) -> None:
        """gRPC ReportConveyorEvent 호출.

        2026-05-06 (SPEC-MGMTV2-INSPECTION-PUSH): tof2_exit 가 accepted 되면
        곧바로 USB 카메라 1 frame 캡처 후 UploadInspectionImage 로 push.
        capture/upload 실패는 ConveyorEvent 자체 결과에 영향 없음 (best-effort).
        """
        from generated import management_pb2  # type: ignore

        req = management_pb2.ConveyorEvent(
            res_id=item.res_id,
            event_type=item.event_type,
            occurred_at=management_pb2.Timestamp(
                iso8601=time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",
                    time.gmtime(item.occurred_at_millis / 1000.0),
                ),
            ),
            idempotency_key=item.idempotency_key,
        )
        resp = stub.ReportConveyorEvent(req, timeout=3.0)
        log.info(
            "ReportConveyorEvent 응답: accepted=%s item_id=%s cur_stat=%s reason=%s",
            resp.accepted,
            resp.item_id,
            resp.item_cur_stat,
            resp.reason,
        )

        # tof2_exit + accepted → INSP 진입 → on-demand capture + push
        if (
            item.event_type == "tof2_exit"
            and resp.accepted
            and int(resp.item_id) > 0
        ):
            self._send_inspection_image_after_tof2(
                stub,
                item_id=int(resp.item_id),
                item_cur_stat=resp.item_cur_stat or "IP",
                idempotency_seed=item.idempotency_key,
            )

    # ---------- Inspection Image Push (ToF2 trigger · on-demand) ----------
    def _send_inspection_image_after_tof2(
        self,
        stub,
        *,
        item_id: int,
        item_cur_stat: str,
        idempotency_seed: str,
    ) -> None:
        """USB 카메라 1 frame 캡처 → UploadInspectionImage push.

        실패는 로깅만. ConveyorEvent (state transition) 은 이미 server 에서 commit 됨.
        env MGMT_CAM_INSP_DISABLED=1 이면 capture 자체 skip (Mac dev / CI).
        """
        if os.environ.get("MGMT_CAM_INSP_DISABLED", "0").strip() == "1":
            log.info("[inspection_push] MGMT_CAM_INSP_DISABLED=1 → skip item=%s", item_id)
            return

        from generated import management_pb2  # type: ignore
        from capture_inspection import capture_jpeg  # type: ignore

        camera_id = os.environ.get("MGMT_CAM_INSP_CAMERA_ID", "CAM-INSP-01").strip() or "CAM-INSP-01"
        stage = (item_cur_stat or "IP").strip() or "IP"

        try:
            cap = capture_jpeg()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "[inspection_push] capture_jpeg() 예외 — skip item=%s camera=%s: %s",
                item_id, camera_id, exc,
            )
            return

        if not cap.ok or not cap.jpeg_bytes:
            log.warning(
                "[inspection_push] capture 실패 — skip item=%s camera=%s reason=%s",
                item_id, camera_id, cap.reason or "<unknown>",
            )
            return

        captured_iso = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(cap.captured_at_unix),
        )
        # idempotency_key 는 conveyor event key + item + camera + ts 로 합쳐 일의성 확보
        ts_ms = int(cap.captured_at_unix * 1000)
        idem = f"{idempotency_seed}:img:{item_id}:{camera_id}:{ts_ms}"

        req = management_pb2.InspectionImageUpload(
            item_id=item_id,
            camera_id=camera_id,
            stage=stage,
            jpeg_bytes=cap.jpeg_bytes,
            captured_at=management_pb2.Timestamp(iso8601=captured_iso),
            idempotency_key=idem,
        )

        try:
            ack = stub.UploadInspectionImage(req, timeout=10.0)
            log.info(
                "[inspection_push] UploadInspectionImage 응답 item=%s camera=%s "
                "accepted=%s stored=%s reason=%s size=%dB",
                item_id, camera_id, ack.accepted, ack.stored_path, ack.reason,
                len(cap.jpeg_bytes),
            )
        except Exception as exc:  # noqa: BLE001 — gRPC 실패는 치명적 아님
            log.warning(
                "[inspection_push] UploadInspectionImage 실패 — item=%s camera=%s size=%dB: %s",
                item_id, camera_id, len(cap.jpeg_bytes), exc,
            )

    # ---------- RFID scan drain (SPEC-RFID-001 · 2026-05-07) ----------
    def _rfid_drain_loop(self) -> None:
        """큐에 쌓인 RFID 스캔 이벤트를 Management gRPC ReportRfidScan 으로 전송.

        지수 백오프 1s → 60s. handoff/conveyor 와 동일 패턴.
        시나리오 A: RFID 스캔이 TOF1 보다 항상 먼저 도달해야 백엔드 PP→ToINSP 전이가 일어남.
        """
        backoff = HANDOFF_BACKOFF_START_S
        stub = self._build_grpc_stub()

        while not self._shutdown.is_set():
            with self._rfid_lock:
                if not self._rfid_queue:
                    item: PendingRfidScan | None = None
                else:
                    item = self._rfid_queue[0]

            if item is None:
                self._shutdown.wait(0.5)
                continue

            if self._grpc_disabled or stub is None:
                log.info(
                    "RFID drain: gRPC disabled/unavailable — 큐 유지 (key=%s)",
                    item.idempotency_key,
                )
                self._shutdown.wait(5.0)
                if not self._grpc_disabled:
                    stub = self._build_grpc_stub()
                continue

            try:
                self._send_rfid_scan(stub, item)
                with self._rfid_lock:
                    if self._rfid_queue and self._rfid_queue[0] is item:
                        self._rfid_queue.popleft()
                log.info("RFID 전송 성공 key=%s", item.idempotency_key)
                backoff = HANDOFF_BACKOFF_START_S
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "RFID 전송 실패 (backoff %.1fs): %s key=%s",
                    backoff,
                    e,
                    item.idempotency_key,
                )
                self._shutdown.wait(backoff)
                backoff = min(backoff * 2, HANDOFF_BACKOFF_CAP_S)
                stub = self._build_grpc_stub()

    def _send_rfid_scan(self, stub, item: PendingRfidScan) -> None:
        """gRPC ReportRfidScan 호출. 실패 시 예외 전파 (drain 루프가 재시도)."""
        from generated import management_pb2  # type: ignore

        req = management_pb2.RfidScanEvent(
            reader_id=item.reader_id,
            zone=item.zone,
            raw_payload=item.raw_payload,
            scanned_at=management_pb2.Timestamp(
                iso8601=time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",
                    time.gmtime(item.occurred_at_millis / 1000.0),
                ),
            ),
            idempotency_key=item.idempotency_key,
        )
        resp = stub.ReportRfidScan(req, timeout=3.0)
        log.info(
            "ReportRfidScan 응답 accepted=%s parse=%s item_id=%s ord_id=%s "
            "cur_stat=%s pp_options=%d reason=%s",
            resp.accepted,
            resp.parse_status,
            resp.item_id,
            resp.ord_id,
            resp.item_cur_stat,
            len(resp.pp_options) if hasattr(resp, "pp_options") else 0,
            resp.reason,
        )
