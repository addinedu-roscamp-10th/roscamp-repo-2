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

from services.query.item_query_service import ItemQueryService
from services.query.pattern_query_service import PatternQueryService
from services.query.production_order_query_service import ProductionOrderQueryService
from services.query.schedule_query_service import ScheduleQueryService
from services.core.pattern_command_service import PatternCommandService


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
        self.adapter = AdapterRouter(ros2_runtime=self.ros2_runtime)
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

    def start(self) -> None:
        """서버 시작 시 adapter들을 실행한다."""
        if self._started:
            return
        logger.info("Starting Dependency Container resources...")
        self.ros2_runtime.start()  # ros2 multi thread 시작
        self.adapter.start()
        self.amr_state_monitor.start()
        self._started = True

    def close(self) -> None:
        """서버 종료 시 실행 중인 adapter들을 정리한다."""
        if not self._started:
            return
        logger.info("Stopping Dependency Container resources...")
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
