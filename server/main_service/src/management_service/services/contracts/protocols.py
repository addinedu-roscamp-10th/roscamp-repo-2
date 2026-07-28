
from typing import Protocol, Dict, Any, List, Optional, Callable

from .enums import EventType, TaskType
from .models import *
from collections.abc import Coroutine


class IOrchestrator(Protocol):
    async def start_production(self, order_ids: List[int]) -> StartProductionBatchAckModel:
        ...

    async def on_task_completed(self, event) -> None:
        ...

    async def on_subtask_completed(self, event) -> None:
        ...

    async def on_amr_charged(self, event) -> None:
        ...

    async def start_shipping(self, ord_id: int | None = None) -> List[int]:
        ...

class ITaskManager(Protocol):

    ##orchestrator가 사용하는 인터페이스
    async def reserve_rack_slots(self, order_id: int, start_pos: tuple[int, int], target_qty:int) -> int:
        ...
    
    async def create_next_task(self, item_info: ItemStatusRecord ,eventMsg: Optional[str] = None) -> List[NextTaskResult]: 
        ...

    async def create_ship_task( self, order_id: int, item_locations: List[tuple[int, int, int]], event: Optional[str] = None,) -> ShipTaskResult | None:
        ...

    def pop_finished_ship_batch(self, order_id: int) -> List[tuple[int, int, int]]:
        ...

    def has_ship_plan(self, order_id: int) -> bool:
        ...

    def release_shipped_slots(self, batch: list[tuple[int, int, int]]) -> None:
        ...

class ITaskAllocator(Protocol):
    async def allocate(self, task: AllocateTaskInput) -> AllocateTaskResult:
        ...

class ITaskExecutor(Protocol):
    async def execute_task(self, input_data: ExecuteTaskInput) -> ExecutionResult:
        ...

    async def handle_emergency_return(self, item_id: int, amr_id: str, arm_id: str) -> None:
        ...

    async def return_amr_to_charger(self, res_id: str, source: str | None = None) -> bool:
        ...

class IAdapter(Protocol):
    
    ##Task Executor가 사용하는 인터페이스
    async def send_command(self, res_id: str, action: str, params: Dict) -> AdapterResult: 
        ...


class IStateManager(Protocol):
    """    
    def start_production(self, ord_id: int) -> StartProductionOrderAckModel:
        ...

    def create_order_with_items(self, ord_id: int, qty: int) -> List[int]:
        ...

    def find_ship_ready_item_ids(self, ord_id: int | None = None, item_ids: List[int] | None = None) -> List[int]:
        ...

    def get_item(self, item_id: int) -> Dict[str, Any]:
        ...
        
    def add_task(self, task: Dict[str, Any]) -> str:
        ...
        
    def update_task_allocation(self, assign_input: AllocateTaskResInput) -> None:
        ...

    
    def update_task_status(self, task_id: str, status: str, is_trans: bool) -> None:
        ...

    async def update_item_status(
        self,
        item_id: int,
        flow_stat: str | None = None,
        zone_nm: str | None = None,
        result: bool | None = None,
    ) -> None:
        ...
        
    def update_res_status_memory(self, res_id: str, x: float, y: float, battery_pct: int) -> None:
        ...

"""
        
    ##Orchestrator가 사용하는 인터페이스
    async def start_production(self, ord_id: int) -> StartProductionOrderAckModel:
        ...

    async def get_item(self, item_id: int) -> ItemStatusRecord:
        ...

    async def get_items_by_order(self, ord_id: int) -> list[ItemStatusRecord]:
        ...
    async def complete_shipping_order_if_ready(self, ord_id: int) -> bool:
        ...
        
    ##Task Allocator가 사용하는 인터페이스
    def get_available_resources(self, req_res_type: str) -> list[str]:
        ...

    def get_amr_stats(self) -> list[AmrRuntimeState]:
        ...

    def get_all_robot_states(self) -> list[dict[str, Any]]:
        ...

    async def update_task_allocation(self, assign_input: AllocateTaskResInput) -> None:
        ...

    def is_res_available(self, res_id: str) -> bool:
        ...

    def get_task_id_for_resource(self, res_id: str) -> str | None:
        ...

    def update_amr_runtime_memory(
        self,
        res_id: str,
        *,
        x: float | None = None,
        y: float | None = None,
        battery_pct: int | None = None,
    ) -> None:
        """ROS telemetry를 메모리에 반영한다."""
        ...

    async def sync_resource_telemetry(self, telemetry: dict[str, Any]) -> None:
        """ROS telemetry의 배터리 정보만 repository에 반영한다."""
        ...

    async def update_amr_charger_return_state(
        self,
        res_id: str,
        status: str,
        source: str | None = None,
    ) -> None:
        ...

    ##Task Executor가 사용하는 인터페이스
    async def update_task_status(self, req: UpdateTaskStatusInput) -> bool: 
        ...    

    async def record_adapter_result(self, req: AdapterStepResultInput) -> bool:
        ...

    async def publish_subtask_completed(
        self,
        *,
        task_id: str,
        item_id: int | None,
        subtask_type: str,
        task_type: TaskType,
    ) -> bool:
        ...

    async def publish_amr_charged(
        self,
        *,
        res_id: str,
        task_id: str | None = None,
        item_id: int | None = None,
        source: str | None = None,
    ) -> bool:
        ...

    ##Task Manager가 사용하는 인터페이스
    items: Dict[int, ItemStatusRecord]
    orders: Dict[int, dict]
    slot_table: Dict[tuple, dict]

    async def insert_task_txn(self, task_input: CreateTaskInput) -> int:
        ...
    
    async def create_empty_item(self, order_id: int) -> int:
        ...

    async def get_empty_start_slot(self, target_qty: int) -> tuple[int, int] | None:
        ...

    async def get_order_target_qty(self, ord_id: int) -> int | None:
        ...

    async def reserve_storage_slots(
        self,
        start_pos: tuple[int, int],
        target_qty: int,
    ) -> int | None:
        ...

    async def update_item_storage_location(
        self,
        item_id: int,
        strg_loc: tuple[int, int] | str,
    ) -> None:
        ...

    async def get_empty_charger(self, res_id: str | None = None) -> tuple[int, int] | None:
        ...
    

class IEventBridge(Protocol):
    
    ##Task Executor가 사용하는 인터페이스
    def publish(self, event: Event) -> PublishResult: 
        ...
    def subscribe(self, event_type: EventType, handler: Callable[[Event], Coroutine[Any, Any, None] | None], subscriber_name: str) -> None:
        ...
    def unsubscribe(self, event_type: EventType, subscriber_name: str) -> bool: 
        ...
    def list_subscribers(self, event_type: EventType | None = None) -> list[HandlerMeta]: 
        ...
