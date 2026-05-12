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

    def _register_pp_done_motor_run(self) -> None:
        """PP_DONE_REQUESTED → 컨베이어 1회차 motor RUN 발신.

        흐름:
            PyQt ③ 후처리 완료 버튼
              → EventGateway PublishEvent(PP_DONE_REQUESTED)
              → 본 핸들러 → command_queue.enqueue("start", "CONV-01")
              → backend WatchConveyorCommands stream (hardware_rpc.py:151)
              → Jetson CommandSubscriber (command_subscriber.py:164)
              → EspBridge.send_command("start") (펌웨어 IDLE → RUNNING)
              → motor ON → 주물 이동 → TOF1 detect → STOPPED → Jetson 캡처
              → UploadInspectionImage → INSP_IMAGE_RECEIVED
              → task_executor INSP task → AI/DB chain.

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
