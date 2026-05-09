from __future__ import annotations

import asyncio
import logging
import threading
from collections import defaultdict
from typing import Dict, List
from services.contracts.models import *
from services.contracts.protocols import IAdapter, IEventBridge, IStateManager


class TaskExecutor:
    def __init__(
        self,
        adapter: IAdapter,
        state_manager: IStateManager,
        event_bridge: IEventBridge | None = None,
    ):
        self.adapter = adapter
        self.state_manager = state_manager
        self.event_bridge = event_bridge
        self.logger = logging.getLogger(__name__)
        self._waiters_lock = threading.Lock()
        self._task_waiters: dict[tuple[int, TaskType], list[asyncio.Future[str]]] = defaultdict(list)
        self._subtask_waiters: dict[tuple[int, str], list[asyncio.Future[None]]] = defaultdict(list)
        self._completed_subtasks: set[tuple[int, str]] = set()

        if self.event_bridge is not None:
            self.event_bridge.subscribe(
                EventType.TASK_COMPLETED,
                self._on_task_completed,
                "task_executor.task_completed_waiters",
            )
            self.event_bridge.subscribe(
                EventType.SUBTASK_COMPLETED,
                self._on_subtask_completed,
                "task_executor.subtask_completed_waiters",
            )
        
        self._sequence_map: Dict[TaskType, List[CommandStep]] = {
            # === MAT (Casting Robot) ===
            TaskType.MM: [
                CommandStep(step_id=1, action="pattern_pick_action", params={}),
                CommandStep(step_id=2, action="die_stamp_action", params={}),
                CommandStep(step_id=3, action="pattern_return_action", params={}),
                CommandStep(step_id=4, action="return_to_base_action", params={}),
            ],
            TaskType.POUR: [
                CommandStep(step_id=1, action="crucible_pick_action", params={}),
                CommandStep(step_id=2, action="metal_pour_action", params={}),
                CommandStep(step_id=3, action="crucible_return_action", params={}),
                CommandStep(step_id=4, action="return_to_base_action", params={}),
            ],
            TaskType.DM: [
                CommandStep(step_id=1, action="casting_pick_action", params={}),
                CommandStep(step_id=2, action="tat_load_action", params={}),
                CommandStep(step_id=3, action="return_to_base_action", params={}),
            ],
            # === PAT (Logistics Robot) ===
            # dld 후 GP 적재
            TaskType.PA_GP: [
                CommandStep(step_id=1, action="pat_pick_action", params={}),
                CommandStep(step_id=2, action="pat_place_storage_action", params={}),
            ],
            # dld 후 DP 적재
            TaskType.PA_DP: [
                CommandStep(step_id=1, action="pat_pick_action", params={}),
                CommandStep(step_id=2, action="pat_defect_drop_action", params={}),
            ],
            # ld 출고
            TaskType.PICK: [
                CommandStep(step_id=1, action="pat_retrieve_action", params={}),
            ],
            # ld SHIP은 현재 없음
            TaskType.SHIP: [
                CommandStep(step_id=1, action="pat_retrieve_action", params={}),
            ],
            # === TAT (AMR) ===
            TaskType.ToPP: [
                CommandStep(step_id=1, action="dock_robot", params={"pose_name": "ToCAST1"}),  # Casting zone 상차 대기 장소
                CommandStep(
                    step_id=2,
                    action="WAIT_TASK_COMPLETED",
                    params={"task_type": TaskType.DM},
                    timeout_sec=600,
                ),
                CommandStep(step_id=3, action="dock_robot", params={"pose_name": "ToPP1"}),    # PP Zone 하차 대기 장소
                # 외부 이벤트 일단 주석 처리
                # CommandStep(
                #     step_id=4,
                #     action="WAIT_SUBTASK_COMPLETED",
                #     params={"subtask_type": "button_pushed"},
                #     timeout_sec=600,
                # ),
                CommandStep(step_id=4, action="dock_robot", params={"pose_name": "ToCHG"}),
            ],
            TaskType.ToSTRG: [
                CommandStep(step_id=1, action="dock_robot", params={"pose_name": "ToINSP"}),   # 컨베이어 상차 대기 장소
                CommandStep(
                    step_id=2,
                    action="WAIT_TASK_COMPLETED",
                    params={"task_type": TaskType.ToWaitPA},
                    timeout_sec=600,
                ),
                CommandStep(step_id=3, action="dock_robot", params={"pose_name": "ToSTRG1"}),  # STRG zone 하차 대기 장소
                CommandStep(
                    step_id=4,
                    action="WAIT_SUBTASK_COMPLETED",
                    params={"subtask_type": "pa_dld_done"},
                    timeout_sec=600,
                ),
                CommandStep(step_id=5, action="dock_robot", params={"pose_name": "ToCHG"}),
            ],
            TaskType.ToSHIP: [
                CommandStep(step_id=1, action="dock_robot", params={"pose_name": "ToSTRG2"}),  # STRG Zone 상차 대기 장소
                CommandStep(
                    step_id=2,
                    action="WAIT_TASK_COMPLETED",
                    params={"task_type": TaskType.PICK},
                    timeout_sec=600,
                ),
                CommandStep(step_id=3, action="dock_robot", params={"pose_name": "ToSHIP"}),   # SHIP Zone 하차 대기 장소
                CommandStep(step_id=4, action="dock_robot", params={"pose_name": "ToCHG"}),
            ],
            TaskType.ToCHG: [
                CommandStep(step_id=1, action="dock_robot", params={"pose_name": "ToCHG"}),   # Charging Zone
            ],
            
            # === CONV (Conveyor Belt) ===
            # 후처리 시작 -> pyqt 후처리 UI의 버튼 클릭이 종료 조건
            TaskType.PP: [
                CommandStep(step_id=1, action="NOOP", params={})
                # 외부 이벤트 임시 주석 처리
                # CommandStep(
                #     step_id=1,
                #     action="WAIT_SUBTASK_COMPLETED",
                #     params={"subtask_type": "pyqt 후처리 UI에서 clicked"},
                #     timeout_sec=600,
                # ),
            ],
            # 컨베이어 이동 후 이미지 수신이 종료 조건
            TaskType.ToINSP: [
                CommandStep(step_id=1, action="NOOP", params={})
                # 외부 이벤트 임시 주석 처리
                # CommandStep(
                #     step_id=2,
                #     action="WAIT_SUBTASK_COMPLETED",
                #     params={"subtask_type": "image_arrived"},
                #     timeout_sec=600,
                # ),
            ],
            # INSP은 수신된 이미지로 AI에 추론 요청을 보내고 추론 결과 수신이 종료 조건
            TaskType.INSP: [
                CommandStep(step_id=1, action="AI_INFERENCE_REQUEST", params={}),
            ],
            TaskType.ToWaitPA: [
                CommandStep(step_id=1, action="NOOP", params={}),
                # 임시 주석 처리
                # CommandStep(
                #     step_id=1,
                #     action="WAIT_TASK_COMPLETED",
                #     params={"task_type": TaskType.INSP},
                #     timeout_sec=600,
                # ),
                CommandStep(step_id=2, action="CONV_ALLOW_MOVE", params={"duration_sec": 4}),
            ],
        }

    async def execute_task(self, input_data: ExecuteTaskInput) -> ExecutionResult:
        """
        메인 실행 파이프라인
        
        1. 전처리 및 Task 진행 상태 업데이트 (QUE -> PROC)
        2. 시퀀스 분해
        3. 단계별 Adapter 호출 및 모니터링
        3.1 특정 task나 subtask가 선행 조건이 되어야하는 step에 대해서는, waiter를 만들어 event가 올 때까지 대기한다.
        4. 최종 결과 보고 (SUCC/FAIL) -> State Manager 업데이트 요청 후 종료

        waiter는 특정 완료 이벤트를 기다리는 future이고, 이벤트 도착 시 set_result()로 완료시켜서 대기 중인 실행을 재개시킴
        """
        self.logger.info(f"[Executor] Start Task: {input_data.task_id} ({input_data.task_type.value})")

        # 1. 전처리: 실행 가능 여부 확인 (Mock)
        # 실제 구현 시에는 res_stat 확인 등 수행
        if not await self._pre_check(input_data):
            return await self._handle_error(input_data, "PRECHECK_FAILED", 0)

        # Task 시작 시 진행 상태 전이: QUE -> PROC
        await self.state_manager.update_task_status(
            UpdateTaskStatusInput(task_id=input_data.task_id, new_stat=TxnStat.PROC)
        )
        await asyncio.sleep(1)

        # 2. 시퀀스 분해
        try:
            sequence = self._breakdown_sequence(input_data.task_type)
        except ValueError as e:
            return await self._handle_error(input_data, str(e), 0)

        executed_steps = 0
        
        # 3. 순차 실행
        try:
            for step in sequence:
                # 단계 실행
                step_result = await self._execute_step(input_data, step)
                if not step_result.success:
                    raise RuntimeError(step_result.message or f"Adapter failed at step {step.step_id}")
                
                executed_steps += 1
                self.logger.info(f"[Executor] Step {step.step_id} completed")
                await self._handle_step_completion(input_data, step, step_result)
            
            # Task가 성공한 경우 전이: PROC -> SUCC
            await self.state_manager.update_task_status(
                UpdateTaskStatusInput(task_id=input_data.task_id, new_stat=TxnStat.SUCC)
            )
            # asyncio.create_task()로 만들어진 코루틴 객체이기 때문에, return은 현재 사용되지 않음
            return ExecutionResult(
                task_id=input_data.task_id,
                final_status=TxnStat.SUCC,
                steps_executed=executed_steps
            )

        except Exception as e:
            # Task가 실패한 경우 전이: PROC -> FAIL
            return await self._handle_error(input_data, str(e), executed_steps)

    async def _pre_check(self, input_data: ExecuteTaskInput) -> bool:
        """실행 전 조건 확인 (Mock)"""
        # TODO: 실제 구현 시 res_id 의 상태 (IDLE 등) 체크
        return True

    def _breakdown_sequence(self, task_type: TaskType) -> List[CommandStep]:
        """Task Type 기준으로 시퀀스를 분해한다."""
        seq = self._sequence_map.get(task_type)
        if not seq:
            raise ValueError(f"No sequence defined for task_type: {task_type.value}")
        return seq.copy()

    async def _execute_step(self, input_data: ExecuteTaskInput, step: CommandStep) -> AdapterResult:
        """
        단일 단계 실행 및 Adapter를 호출한다.
        
        특정 선행 단계(task, subtask)를 기다려야할 경우 waiter를 만들고 완료 event를 기다린다.
        """
        if step.action == "WAIT_TASK_COMPLETED":
            await self._wait_for_task_completed(input_data, step)
            return AdapterResult(success=True, message="wait_task_completed")
        if step.action == "WAIT_SUBTASK_COMPLETED":
            await self._wait_for_subtask_completed(input_data, step)
            return AdapterResult(success=True, message="wait_subtask_completed")
        if step.action == "NOOP": # 테스트용 fake action
            return AdapterResult(success=True, message="noop")
        return await self.adapter.send_command(
            res_id=input_data.res_id,
            action=step.action,
            params={**step.params, **input_data.payload},
        )

    async def _wait_for_task_completed(self, input_data: ExecuteTaskInput, step: CommandStep) -> bool:
        """특정 task 완료를 기다리기 위해 waiter를 등록하고 대기한다."""
        item_id = input_data.item_id
        if item_id is not None:
            item_id = int(item_id)
        if item_id is None:
            raise RuntimeError("WAIT_TASK_COMPLETED requires item_id")

        target_task_type = step.params.get("task_type")
        if isinstance(target_task_type, str):
            try:
                target_task_type = TaskType(target_task_type)
            except ValueError as exc:
                raise RuntimeError("WAIT_TASK_COMPLETED requires params.task_type") from exc
        if not isinstance(target_task_type, TaskType):
            raise RuntimeError("WAIT_TASK_COMPLETED requires params.task_type")

        key = (item_id, target_task_type)
        loop = asyncio.get_running_loop()  # 현재 스레드에서 돌고 있는 event loop를 받아옴
        future: asyncio.Future[str] = loop.create_future()  # future 객체 생성
        with self._waiters_lock:
            self._task_waiters[key].append(future)  # item id와 task type으로 waiter 저장
        self.logger.info(
            "[Executor] Waiting for task completion: task=%s item=%s target=%s timeout=%ss",
            input_data.task_id,
            item_id,
            key[1],
            step.timeout_sec,
        )

        try:
            status = await asyncio.wait_for(future, timeout=step.timeout_sec)  # waiter의 future를 대기시킴. task, subtask completed에서 resolve(done)시킴
            self.logger.info(
                "[Executor] Wait released: task=%s item=%s target=%s status=%s",
                input_data.task_id,
                item_id,
                key[1],
                status,
            )
            if status != TxnStat.SUCC.value:
                raise RuntimeError(
                    f"Upstream task finished with status={status}: item_id={item_id} task_type={key[1]}"
                )
            return True
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"Timed out waiting for task completion: item_id={item_id} task_type={key[1]}"
            ) from exc
        finally:
            self._remove_waiter(key, future)

    async def _wait_for_subtask_completed(self, input_data: ExecuteTaskInput, step: CommandStep) -> bool:
        """특정 subtask 완료를 기다리기 위해 waiter를 등록하고 대기한다."""
        item_id = int(input_data.item_id) if input_data.item_id is not None else None
        if item_id is None:
            raise RuntimeError("WAIT_SUBTASK_COMPLETED requires item_id")

        subtask_type = step.params.get("subtask_type")
        if not isinstance(subtask_type, str) or not subtask_type.strip():
            raise RuntimeError("WAIT_SUBTASK_COMPLETED requires params.subtask_type")

        key = (item_id, subtask_type.strip())
        # task 생성 전에 먼저 온 이벤트를 보고 성공처리
        if key in self._completed_subtasks:
            self.logger.info(
                "[Executor] Subtask already observed: task=%s item=%s subtask_type=%s",
                input_data.task_id,
                item_id,
                key[1],
            )
            return True

        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        with self._waiters_lock:
            self._subtask_waiters[key].append(future)

        self.logger.info(
            "[Executor] Waiting for subtask completion: task=%s item=%s subtask_type=%s timeout=%ss",
            input_data.task_id,
            item_id,
            key[1],
            step.timeout_sec,
        )
        try:
            await asyncio.wait_for(future, timeout=step.timeout_sec)
            self.logger.info(
                "[Executor] Subtask wait released: task=%s item=%s subtask_type=%s",
                input_data.task_id,
                item_id,
                key[1],
            )
            return True
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"Timed out waiting for subtask completion: item_id={item_id} subtask_type={key[1]}"
            ) from exc
        finally:
            self._remove_subtask_waiter(key, future)

    async def _handle_step_completion(
        self,
        input_data: ExecuteTaskInput,
        step: CommandStep,
        step_result: AdapterResult,
    ) -> None:
        """특정 subtask 완료 시 StateManager에게 알린다."""
        subtask_type = None
        pose_name = step.params.get("pose_name")
        if input_data.task_type == TaskType.POUR and step.action == "metal_pour_action":
            subtask_type = "pour"
        elif input_data.task_type == TaskType.ToPP and step.action == "dock_robot" and pose_name == "ToCAST1":
            subtask_type = "topp"
        elif input_data.task_type == TaskType.ToSTRG and step.action == "dock_robot" and pose_name == "ToINSP":
            subtask_type = "tostrg"
        elif input_data.task_type == TaskType.ToSTRG and step.action == "dock_robot" and pose_name == "ToSTRG1":
            subtask_type = "tostrg_dld"
        elif input_data.task_type == TaskType.PA_GP and step.action == "pat_place_storage_action":
            subtask_type = "pa_dld_done"
        elif input_data.task_type == TaskType.PA_DP and step.action == "pat_defect_drop_action":
            subtask_type = "pa_dld_done"
        if subtask_type is None:
            return

        item_id = int(input_data.item_id) if input_data.item_id is not None else None
        await self.state_manager.publish_subtask_completed(
            task_id=input_data.task_id,
            item_id=item_id,
            subtask_type=subtask_type,
            task_type=input_data.task_type,
        )

    async def _handle_error(self, input_data: ExecuteTaskInput, error_msg: str, steps: int) -> ExecutionResult:
        """에러 처리 및 상태 업데이트"""
        self.logger.error(f"[Executor] Task {input_data.task_id} failed: {error_msg}")
        await self.state_manager.update_task_status(
            UpdateTaskStatusInput(task_id=input_data.task_id, new_stat=TxnStat.FAIL, error_code=error_msg)
        )
        return ExecutionResult(
            task_id=input_data.task_id,
            final_status=TxnStat.FAIL,
            steps_executed=steps,
            error_code=error_msg
        )

    def _on_task_completed(self, event: Event) -> None:
        """
        특정 task가 끝나야 실행되는 subtask를 깨우는 handler.

        TASK_COMPLETED 종료 이벤트를 받으면 그 task를 기다리던 waiter 목록을 key로 찾고
        그 waiter들을 깨워서 WAIT_TASK_COMPLETED 다음 step으로 넘어가게 한다.
        """
        item_id = event.item_id
        payload_task_type = event.payload.get("task_type")
        payload_status = event.payload.get("status", TxnStat.SUCC.value)
        if item_id is None or not isinstance(payload_task_type, TaskType):
            return

        key = (item_id, payload_task_type)
        with self._waiters_lock:
            futures = self._task_waiters.pop(key, [])

        for future in futures:
            if future.done():
                continue
            future.get_loop().call_soon_threadsafe(self._resolve_task_waiter, future, payload_status)

    def _on_subtask_completed(self, event: Event) -> None:
        """
        특정 subtask가 끝나야 실행되는 subtask를 깨우는 handler.
        
        SUBTASK_COMPLETED 이벤트를 받으면 그 task를 기다리던 waiter 목록을 key로 찾고
        그 waiter들을 깨워서 WAIT_TASK_COMPLETED 다음 step으로 넘어가게 한다.
        """
        item_id = event.item_id
        payload_subtask_type = event.payload.get("subtask_type")
        if item_id is None or payload_subtask_type is None:
            return

        key = (item_id, payload_subtask_type)
        self._completed_subtasks.add(key)  # 먼저 온 subtask를 저장해두기
        with self._waiters_lock:
            futures = self._subtask_waiters.pop(key, [])

        for future in futures:
            if future.done():
                continue
            future.get_loop().call_soon_threadsafe(self._resolve_waiter, future)

    def _resolve_task_waiter(self, future: asyncio.Future[str], status: str) -> None:
        """task waiter의 future를 완료시키고 upstream task status를 전달한다."""
        if not future.done():
            future.set_result(status)

    def _resolve_waiter(self, future: asyncio.Future[None]) -> None:
        """waiter의 future를 완료시킨다."""
        if not future.done():
            future.set_result(None)

    def _remove_waiter(self, key: tuple[int, TaskType], future: asyncio.Future[str]) -> None:
        """task가 끝나는 걸 기다리던 waiter를 제거."""
        with self._waiters_lock:
            futures = self._task_waiters.get(key)
            if not futures:
                return
            self._task_waiters[key] = [registered for registered in futures if registered is not future]
            if not self._task_waiters[key]:
                self._task_waiters.pop(key, None)

    def _remove_subtask_waiter(self, key: tuple[int, str], future: asyncio.Future[None]) -> None:
        """subtask가 끝나는 걸 기다리던 waiter를 제거."""
        with self._waiters_lock:
            futures = self._subtask_waiters.get(key)
            if not futures:
                return
            self._subtask_waiters[key] = [registered for registered in futures if registered is not future]
            if not self._subtask_waiters[key]:
                self._subtask_waiters.pop(key, None)
