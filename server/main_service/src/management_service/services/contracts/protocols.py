
from typing import Protocol, Dict, Any, List, Optional

from .enums import EventType, TaskType
from .models import *


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
    def reserve_rack_slots(self, order_id: int, start_pos: str): 
        ...
    
    async def create_next_task(self, item_info: ItemStatusRecord ,eventMsg: Optional[str] = None) -> List[NextTaskResult]: 
        ...

    def reissue_task_on_error(self, item_info: ItemStatusRecord) -> List[NextTaskResult]: 
        ...

    def get_order_progress(self, order_id: int) : 
     ...

class ITaskAllocator(Protocol):
    async def allocate(self, input_data)  :
        ...

class ITaskExecutor(Protocol):
    def execute_task(self, ExecuteTaskInput):
        ...

class IAdapter(Protocol):
    
    ##Task Executor가 사용하는 인터페이스
    async def send_command(self, res_id: str, action: str, params: Dict) -> bool: 
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
        
    def find_available_res(self, res_type: str, task_type: str | None = None) -> str | None:
        ...

    def get_res_available_for_item(self, res_id: str, item_id: int | None = None) -> bool:
        ...
        
    def update_task_allocation(self, assign_input: AllocateTaskResInput) -> None:
        ...

    
    def update_task_status(self, task_id: str, status: str, is_trans: bool) -> None:
        ...

    def mark_task_started(self, task_id: str, res_id: str, is_trans: bool) -> None:
        ...
        
    def update_item_status(
        self,
        item_id: int,
        flow_stat: str | None = None,
        zone_nm: str | None = None,
        result: bool | None = None,
    ) -> None:
        ...
        
    def update_res_status_memory(self, res_id: str, x: float, y: float, battery_pct: int) -> None:
        ...

    def update_amr_runtime_memory(
        self,
        res_id: str,
        *,
        x: float | None = None,
        y: float | None = None,
        battery_pct: int | None = None,
    ) -> None:
        ...

    def update_res_task_state(self, task_id: str, res_id: str, cur_stat: str) -> None:
        ...
"""
        
    ##Orchestrator가 사용하는 인터페이스
    async def start_production(self, ord_id: int) -> StartProductionOrderAckModel:
        ...

    async def get_item(self, item_id: int) -> ItemStatusRecord:
        ...
        
    ##Task Allocator가 사용하는 인터페이스
    async def get_available_resources(self, req_res_type: str) -> list[str]:
        ...

    async def get_amr_locations(self) -> list[AmrLocationResult]:
        ...

    async def update_task_allocation(self, assign_input: AllocateTaskResInput) -> None:
        ...

    ##Task Executor가 사용하는 인터페이스
    async def update_task_status(self, req: UpdateTaskStatusInput) -> bool: 
        ...    

    async def publish_subtask_completed(
        self,
        *,
        task_id: str,
        item_id: int | None,
        subtask_type: str,
        task_type: TaskType | None = None,
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
    

class IEventBridge(Protocol):
    
    ##Task Executor가 사용하는 인터페이스
    def publish(self, event: Event) -> PublishResult: 
        ...
    def subscribe(self, event_type: EventType, handler: Callable[[Event], None], subscriber_name: str) -> None: 
        ...
    def unsubscribe(self, event_type: EventType, subscriber_name: str) -> bool: 
        ...
    def list_subscribers(self, event_type: EventType | None = None) -> list[HandlerMeta]: 
        ...
