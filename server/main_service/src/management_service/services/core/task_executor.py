from __future__ import annotations

import asyncio
import logging
import threading
from collections import defaultdict
from typing import Dict, List
from services.contracts.models import *
from services.contracts.protocols import IAdapter, IEventBridge, IStateManager


class TaskExecutor:
    _CHARGER_POSE_MAP = {
        (1, 1): "ToCHG1",
        (1, 2): "ToCHG2",
        (1, 3): "ToCHG3",
    }
    _EXTERNAL_WAIT_EVENTS = {
        EventType.HANDOFF_ACK,
        EventType.PP_DONE_REQUESTED,
        EventType.ITEM_LOOKUP_REQUESTED,
    }

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
            for event_type in self._EXTERNAL_WAIT_EVENTS:
                self.event_bridge.subscribe(
                    event_type,
                    self._on_external_wait_event,
                    f"task_executor.external_waiters.{event_type.value.lower()}",
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
                CommandStep(step_id=1, action="dock_robot", params={"pose_name": "ToCAST"}),  # Casting zone 상차 대기 장소
                CommandStep(
                    step_id=2,
                    action="WAIT_TASK_COMPLETED",
                    params={"task_type": TaskType.DM},
                    timeout_sec=600,
                ),
                CommandStep(step_id=3, action="dock_robot", params={"pose_name": "ToPP"}),    # PP Zone 하차 대기 장소
                CommandStep(
                    step_id=4,
                    action="WAIT_SUBTASK_COMPLETED",
                    params={"subtask_type": EventType.HANDOFF_ACK.value},
                    timeout_sec=600,
                ),
            ],
            TaskType.ToSTRG: [
                CommandStep(step_id=1, action="dock_robot", params={"pose_name": "ToINSP"}),   # 컨베이어 상차 대기 장소
                CommandStep(
                    step_id=2,
                    action="WAIT_TASK_COMPLETED",
                    params={"task_type": TaskType.ToPAWait},
                    timeout_sec=600,
                ),
                CommandStep(step_id=3, action="dock_robot", params={"pose_name": "ToSTRG"}),  # STRG zone 하차 대기 장소
                CommandStep(
                    step_id=4,
                    action="WAIT_SUBTASK_COMPLETED",
                    params={"subtask_type": "pa_dld_done"},
                    timeout_sec=600,
                ),
            ],
            TaskType.ToSHIP: [
                CommandStep(step_id=1, action="dock_robot", params={"pose_name": "ToPICK"}),  # STRG Zone 상차 대기 장소
                CommandStep(
                    step_id=2,
                    action="WAIT_TASK_COMPLETED",
                    params={"task_type": TaskType.PICK},
                    timeout_sec=600,
                ),
                CommandStep(step_id=3, action="dock_robot", params={"pose_name": "ToSHIP"}),   # SHIP Zone 하차 대기 장소
                CommandStep(step_id=4, action="WAIT_TIME", params={"duration_sec": 5}),  # 7초 대기로 하차 신호
            ],
            TaskType.ToCHG: [
                CommandStep(step_id=1, action="dock_robot", params={}),  # 실행 전 계산 후 주입
            ],
            
            # === CONV (Conveyor Belt) ===
            # 후처리 시작 -> pyqt 후처리 UI의 버튼 클릭이 종료 조건
            TaskType.PP: [  
                CommandStep(
                    step_id=1,
                    action="WAIT_SUBTASK_COMPLETED",
                    params={"subtask_type": EventType.PP_DONE_REQUESTED.value},
                    timeout_sec=600,
                ),
            ],
            # 컨베이어 이동 후 이미지 수신이 종료 조건
            TaskType.ToINSP: [
                CommandStep(
                    step_id=1,
                    action="WAIT_SUBTASK_COMPLETED",
                    params={"subtask_type": EventType.ITEM_LOOKUP_REQUESTED.value},
                    timeout_sec=600,
                ),
            ],
            # INSP은 수신된 이미지로 AI에 추론 요청을 보내고 추론 결과 수신이 종료 조건
            TaskType.INSP: [
                CommandStep(step_id=1, action="AI_INFERENCE_REQUEST", params={}),
            ],
            TaskType.ToPAWait: [
                CommandStep(step_id=1, action="NOOP", params={}),
                CommandStep(
                    step_id=2,
                    action="WAIT_SUBTASK_COMPLETED",
                    params={"subtask_type": "tostrg"},
                    timeout_sec=600,
                ),
                CommandStep(step_id=3, action="CONV_ALLOW_MOVE", params={"duration_sec": 4}),
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

    async def handle_emergency_return(self, item_id: int, amr_id: str, arm_id: str) -> None:
            """
            비상 상황 시 로봇팔을 안전하게 복구하고 초기화한다.
            1. 현재 실행 중인 로봇팔 태스크 중단 (상태 업데이트)
            2. 하드웨어를 통한 안전 하차 및 홈 위치 복귀
            3. 복구 완료 이벤트 발행
            """
            self.logger.warning(
                f"[Executor] Emergency Return initiated: item={item_id}, amr={amr_id}, arm={arm_id}"
            )

            try:
                # 1. 하드웨어 복구 시퀀스 실행 (Adapter 호출)
                # 시나리오 5단계: 아이템을 안전한 곳에 내려놓고(Place) 홈으로 이동(Home)
                # 실제 어댑터에 해당 액션들이 정의되어 있어야 합니다.

                # 단계 A: 그리퍼 열기 혹은 안전 위치에 하차
                success_place = await self.adapter.send_command(
                    res_id=arm_id,
                    action="EMERGENCY_PLACE_ITEM",
                    params={"item_id": item_id, "speed": 30}
                )

                if not success_place:
                    self.logger.error(f"[Executor] Failed to place item safely for {arm_id}")
                    # 실패하더라도 다음 단계를 시도하거나 에러 처리를 강화할 수 있습니다.

                # 단계 B: 홈 위치로 복귀
                await self.adapter.send_command(
                    res_id=arm_id,
                    action="GO_HOME",
                    params={"speed": 50}
                )

                # 2. 상태 관리자에게 복구 완료 알림 (선택 사항)
                # 필요한 경우 state_manager.update_res_status 등을 통해 리소스 상태를 idle로 변경

                # 3. ARM_RETURN_COMPLETED 이벤트 발행 (오케스트레이터가 대기 중인 신호)
                if self.event_bridge is not None:
                    self.event_bridge.publish(
                        Event(
                            event_type=EventType.ARM_RETURN_COMPLETED,
                            item_id=item_id,
                            res_id=amr_id, # 방전된 amr_id를 전달하여 오케가 식별하게 함
                            payload={
                                "arm_id": arm_id,
                                "status": "COMPLETED",
                                "msg": "Emergency return sequence finished safely"
                            }
                        )
                    )
                    self.logger.info(f"[Executor] ARM_RETURN_COMPLETED event published for item {item_id}")

            except Exception as e:
                self.logger.error(f"[Executor] Error during handle_emergency_return: {str(e)}")
                # 치명적 오류 발생 시 별도의 알람 로직 필요

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
        if step.action == "WAIT_TIME":  # 출고 대기용 시간
            await asyncio.sleep(step.params.get("duration_sec", 0))
            return AdapterResult(success=True, message="wait_time")
        if step.action == "NOOP": # 테스트용 fake action
            return AdapterResult(success=True, message="noop")
        params = {**step.params, **input_data.payload}

        if step.action == "dock_robot" and "pose_name" not in params:
            charger_slot = await self.state_manager.get_empty_charger(input_data.res_id)
            pose_name = self._resolve_charger_pose(charger_slot)
            if pose_name is None:
                return AdapterResult(success=False, message="no_available_charger")
            params["pose_name"] = pose_name
        return await self.adapter.send_command(
            res_id=input_data.res_id,
            action=step.action,
            params=params,
        )

    def _resolve_charger_pose(self, charger_slot: tuple[int, int] | None) -> str | None:
        """chg_loc을 AMR의 dock id로 매핑."""
        if charger_slot is None:
            return None
        return self._CHARGER_POSE_MAP.get(charger_slot)

    def _should_bypass_hardware_execution(self, res_id: str) -> bool:
        """현재는 AMR(TAT)과 ARM(MAT/PAT)만 실제 하드웨어 명령을 실행한다."""
        normalized_res_id = (res_id or "").upper()
        if normalized_res_id in self._HARDWARE_RESOURCE_IDS:
            return False
        if normalized_res_id.startswith(self._HARDWARE_RESOURCE_PREFIXES):
            return False
        return True

    async def return_amr_to_charger(self, res_id: str, source: str | None = None) -> bool:
        """대기 중인 AMR을 남는 충전소로 복귀시킨다."""
        charger_slot = await self.state_manager.get_empty_charger(res_id)
        pose_name = self._resolve_charger_pose(charger_slot)
        if pose_name is None:
            self.logger.info(
                "[Executor] Charger return skipped: no available charger for res_id=%s source=%s",
                res_id,
                source,
            )
            return False



        # 충전소 이동 시 toidle 상태로 변경
        self.state_manager.update_amr_charger_return_state(
            res_id,
            "toidle",
            source=source,
        )
        result = await self.adapter.send_command(
            res_id=res_id,
            action="dock_robot",
            params={"pose_name": pose_name},
        )
        if result.success:
            self.logger.info(
                "[Executor] Charger return dispatched: res_id=%s pose_name=%s source=%s",
                res_id,
                pose_name,
                source,
            )
        else:
            self.logger.warning(
                "[Executor] Charger return failed: res_id=%s pose_name=%s source=%s message=%s",
                res_id,
                pose_name,
                source,
                result.message,
            )
        # 충전소 도착 후 idle 상태로 변경
        self.state_manager.update_amr_charger_return_state(
            res_id,
            "idle",
            source=source,
        )
        return result.success

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
        elif input_data.task_type == TaskType.ToPP and step.action == "dock_robot" and pose_name == "ToCAST":
            subtask_type = "topp"
        elif input_data.task_type == TaskType.ToSTRG and step.action == "dock_robot" and pose_name == "ToINSP":
            subtask_type = "tostrg"
        elif input_data.task_type == TaskType.ToSTRG and step.action == "dock_robot" and pose_name == "ToSTRG":
            subtask_type = "tostrg_dld"
        elif input_data.task_type == TaskType.PA_GP and step.action == "pat_pick_action":
            subtask_type = "pa_dld_done"
        elif input_data.task_type == TaskType.PA_DP and step.action == "pat_pick_action":
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

        key = (item_id, str(payload_subtask_type))
        self._completed_subtasks.add(key)
        with self._waiters_lock:
            futures = self._subtask_waiters.pop(key, [])

        for future in futures:
            if future.done():
                continue
            future.get_loop().call_soon_threadsafe(self._resolve_subtask_waiter, future)

    def _on_external_wait_event(self, event: Event) -> None:
        """외부 이벤트를 WAIT_SUBTASK_COMPLETED waiter와 같은 방식으로 처리한다."""
        if event.item_id is None:
            return
        key = (event.item_id, event.event_type.value)
        self._completed_subtasks.add(key)
        with self._waiters_lock:
            futures = self._subtask_waiters.pop(key, [])

        for future in futures:
            if future.done():
                continue
            future.get_loop().call_soon_threadsafe(self._resolve_subtask_waiter, future)

    def _resolve_task_waiter(self, future: asyncio.Future[str], status: str) -> None:
        """task waiter의 future를 완료시키고 upstream task status를 전달한다."""
        if not future.done():
            future.set_result(status)

    def _resolve_subtask_waiter(self, future: asyncio.Future[None]) -> None:
        """subtask waiter의 future를 완료시키고 upstream task status를 전달한다."""
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
