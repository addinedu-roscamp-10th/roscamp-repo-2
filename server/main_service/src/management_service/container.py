import logging
import os

from services.legacy.execution_monitor import ExecutionMonitor
from services.core.orchestrator import Orchestrator
from services.core.task_manager import TaskManager
from services.core.task_allocator import TaskAllocator
from services.core.task_executor import TaskExecutor
from services.core.adapter import Adapter
from services.core.traffic_manager import TrafficManager
from services.core.mock_state_manager import MockStateManager
from services.core.event_bridge import EventBridge

from services.core.adapters.vision.ai_client import AIServerConfig, AIUploader
from services.core.adapters.vision.image_forwarder import ForwarderConfig, ImageForwarder
from services.core.adapters.vision.image_sink import sink as image_sink
from services.core.adapters.sensors.rfid_service import RfidService
from services.core.adapters.robotics.amr_battery import AmrBatteryService

from services.query.item_query_service import ItemQueryService
from services.query.pattern_query_service import PatternQueryService
from services.query.production_order_query_service import ProductionOrderQueryService
from services.query.schedule_query_service import ScheduleQueryService
from services.core.pattern_command_service import PatternCommandService


logger = logging.getLogger(__name__)

def _build_image_forwarder():
    """ImageForwarder 를 구성. AI Server 설정이 없으면 None 반환 → 훅 비활성."""
    ai_cfg = AIServerConfig.from_env()
    if not ai_cfg.enabled:
        logger.info("image_forwarder 비활성: AI Server 환경변수 미설정")
        return None
    fwd = ImageForwarder(
        config=ForwarderConfig.from_env(),
        sink_latest=image_sink.latest,
        uploader=AIUploader(ai_cfg),
    )
    fwd.start()
    logger.info(
        "image_forwarder 활성: spool=%s batch=%.1fs", fwd.cfg.spool_dir, fwd.cfg.batch_interval_sec
    )
    return fwd

class Container:
    """Dependency Injection Container

    모든 Adapter와 Core 비즈니스 로직을 여기서 생성하고 서로 주입(연결)합니다.
    server.py 에서는 이 container 인스턴스만 import 해서 사용합니다.
    """
    def __init__(self):
        logger.info("Initializing Dependency Container...")

        self.event_bridge = EventBridge()
        self.state_manager = MockStateManager(event_bridge=self.event_bridge)
        self.task_manager = TaskManager(sm=self.state_manager)
        self.task_allocator = TaskAllocator(state_manager=self.state_manager)
        self.adapter = Adapter()
        self.task_executor = TaskExecutor(
            adapter=self.adapter,
            state_manager=self.state_manager,
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
        self.rfid_service = RfidService()
        
        # 3. Vision / Monitor Adapters
        self.image_forwarder = _build_image_forwarder()
        self.execution_monitor = ExecutionMonitor(
            image_forwarder=self.image_forwarder,
        )

        # 4. Robotics Adapters
        self.amr_battery = AmrBatteryService()
        self.amr_battery.start()

# Singleton Container Instance
container = Container()
