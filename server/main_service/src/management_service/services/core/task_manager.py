"""Compatibility shell for the deprecated TaskManager dependency."""

from __future__ import annotations

import logging
from typing import List
from services.contracts.models import *
from services.contracts.protocols import ITaskManager , IStateManager

logger = logging.getLogger(__name__)


class TaskManager(ITaskManager):
    MAX_COL = 6
    MAX_ROW = 3
    
    def __init__(self, sm:IStateManager):
        self.sm = sm
        self.order_start_configs = {}#오더별로 할당된 적재 기준 위치
        self.slot_table: dict[tuple[int, int], dict] = {}  # 메모리 상의 보관랙

        self._init_slot_table()
        #self.order_assigned_counts = {}#오더별로 할당된 양품 개수를 관리

    def _init_slot_table(self):
        """3x6 적재 슬롯 초기화"""
        for row in range(1, self.MAX_ROW + 1):
            for col in range(1, self.MAX_COL + 1):
                self.slot_table[(row, col)] = {
                    "status": "Empty",
                    "order_id": None,
                }

        self.slot_table[(1, 1)]["status"] = "Used"
        self.slot_table[(1, 2)]["status"] = "Used"
        self.slot_table[(1, 3)]["status"] = "Used"


    #오더 투입시 해당 오더가 사용할 적재 공간 예약
    def reserve_rack_slots(self, order_id: int, start_pos: str, target_qty: int):
        """주문 투입 시 해당 오더가 사용할 슬롯들을 가상 랙에 예약한다."""
        row, col = map(int, start_pos.split("-"))

        assigned = 0
        current_abs_idx = (row - 1) * self.MAX_COL + (col - 1)

        while assigned < target_qty:
            curr_row = (current_abs_idx // self.MAX_COL) + 1
            curr_col = (current_abs_idx % self.MAX_COL) + 1

            pos_key = (curr_row, curr_col)

            if pos_key not in self.slot_table:
                logger.warning("랙 공간 부족: %s-%s 위치가 slot_table에 없음", curr_row, curr_col)
                break

            slot = self.slot_table[pos_key]

            if slot.get("status") != "Empty" or slot.get("order_id") is not None:
                logger.warning("예약 불가 슬롯: %s-%s slot=%s", curr_row, curr_col, slot)
                break

            slot["order_id"] = order_id
            slot["status"] = "Reserved"

            assigned += 1
            current_abs_idx += 1

        logger.info("주문 %s 구역 예약 완료: start=%s target=%s assigned=%s",
                    order_id, start_pos, target_qty, assigned)

    def log_slot_table(self):
        """현재 slot_table 상태를 로그 출력"""

        logger.info("============= SLOT TABLE =============")

        for row in range(1, self.MAX_ROW + 1):
            line = []

            for col in range(1, self.MAX_COL + 1):
                slot = self.slot_table[(row, col)]

                status = slot["status"]
                order_id = slot["order_id"]

                if status == "Empty":
                    text = f"{row}-{col}:EMPTY"
                else:
                    text = f"{row}-{col}:{status}(O{order_id})"

                line.append(f"{text:25}")

            logger.info(" ".join(line))

        logger.info("======================================")

    #오더 종료시 오더 적재기준 위치 메모리 해제
    def remove_order_reserve(self, order_id: int):
        """주문이 완전히 종료되면 호출하여 메모리 해제"""
        if order_id in self.order_start_configs:
            del self.order_start_configs[order_id]
            logger.info("주문 %s 설정이 메모리에서 제거되었습니다.", order_id)

    #오더 종료 시 Empty 슬롯의 소유권 해제 (Occupied는 유지 - 출고용)
    def remove_order_config(self, order_id: int):
       
        for (f, c), data in self.slot_table.items():
            if data["order_id"] == order_id:
                if data["status"] in ["Empty", "Reserved"]:
                    data["order_id"] = None
                    data["status"] = "Empty"

    #작업 객체 생성 to orchestrator
    async def create_next_task(self, item_info: ItemStatusRecord, event: str = None) -> List[NextTaskResult]:
        
        # [B] 일반적인 공정 흐름 (자동 전이)
        # 특별한 외부 조건이 없어도 순서에 따라 다음 작업을 자동으로 뱉어줍니다.
        #none -> MM -> pour -> dm     -> pp -> toInsp -> Insp
        #                ㄴ> topp    _|     ㄴ>toStrg -> ToPAWait -> tostrg_dld -> pa gp/dp
        #logger.info("create_next_task 호출: item_info=%s event=%s", item_info, event)

        flow_map = {
            # MM이 끝나면 자동으로 POUR로 넘어감
            TaskType.MM: TaskType.POUR,      
            
            # DM(탈형)이 끝나면 자동으로 PP(가공)로 넘어감
            TaskType.DM: TaskType.PP,        
            
            # PP가 끝나면 자동으로 두 장소로 이송 시작
            TaskType.PP: [TaskType.ToINSP, TaskType.ToSTRG], 
            
            # 이송(ToINSP)이 끝나면 자동으로 INSP(검사) 시작
            TaskType.ToINSP: TaskType.INSP,

            # 추론(INSP)이 끝나면, 자동으로 ToPAWait 시작
            TaskType.INSP: TaskType.ToPAWait,
        }
        
        task_results = []
        
        # [1] 이벤트 기반 특수 공정 분기 (최우선 처리)
        # 이벤트가 들어오면 last_task_type에 상관없이 즉시 해당 로직을 타고 return합니다.
        if event:
            # POUR 세부 공정 중
            if event == "pour": 
                task_results.append(await self._create_result(item_info, TaskType.ToPP))
                return task_results
            # ToPP 세부 공정 중 AMR이 src(CASTING 대기장소)에 도착
            elif event == "topp":
                task_results.append(await self._create_result(item_info, TaskType.DM))
                return task_results
            
            # INSP 이후 tostrg 세부 공정 중 AMR이 src(컨베이어 앞 대기장소)에 도착
            #elif event == "tostrg":
            #    task_results.append(await self._create_result(item_info, TaskType.ToPAWait))
            #    return task_results
            
            # AMR bat low
            # elif event == "amr_battery_low_DM": #tochg = 10 dm =15 topp =15
            #    task_results.append(NextTaskResult(
            #            item_id=item_info.item_id, 
            #            txn_id=await self.sm.insert_task_txn(CreateTaskInput(item_id=item_info.item_id, task_type=TaskType.DM)),
            #            task_type=TaskType.DM, 
            #            priority=15
            #        ))
            #    return task_results
            
            elif event == "amr_battery_low_ToPP": #tochg = 10 dm =15 topp =15
                task_results.append(NextTaskResult(
                        item_id=item_info.item_id, 
                        txn_id=await self.sm.insert_task_txn(CreateTaskInput(item_id=item_info.item_id, task_type=TaskType.ToPP)),
                        task_type=TaskType.ToPP, 
                        priority=15
                    ))
                return task_results
            
            elif event == "amr_battery_low_ToCHG": #tochg = 10 dm =15 topp =15   주차자리 정하기
                chg_loc = await self.sm.get_empty_charger(item_info.req_res_id) ####사용가능한 주차 자리 1개 반환 및 예약
                if chg_loc is None:
                    logger.warning(
                        "charger 예약 실패: item_id=%s res_id=%s",
                        item_info.item_id,
                        item_info.req_res_id,
                    )
                task_results.append(NextTaskResult(
                        item_id=item_info.item_id, 
                        txn_id=await self.sm.insert_task_txn(CreateTaskInput(item_id=item_info.item_id, task_type=TaskType.ToCHG)),
                        task_type=TaskType.ToCHG, 
                        priority=10,
                        chg_loc=chg_loc
                    ))
                return task_results
            
            # ToSTRG 세부 공정 중 AMR이 src(적재 대기장소)에 도착
            elif event == "tostrg_dld":
                if item_info.is_defective: 
                    # 1. 불량 보충 아이템 생성 및 MM 태스크 발행 (우선순위 10)
                    replacement_id = await self.sm.create_empty_item(item_info.order_id)
                    task_results.append(NextTaskResult(
                        item_id=item_info.item_id, 
                        txn_id=await self.sm.insert_task_txn(CreateTaskInput(item_id=replacement_id, task_type=TaskType.MM)),
                        task_type=TaskType.MM, 
                        priority=10
                    ))

                    # 2. [Slot Table 반영] 현재 불량 제품이 예약했던 슬롯을 다시 Empty로 변경
                    # item_info 혹은 현재 할당된 slot_table에서 'Reserved' 상태인 내 칸을 찾아 해제합니다.
                    for (f, c), data in self.slot_table.items():
                        if data["order_id"] == item_info.order_id and data["status"] == "Reserved":
                            data["status"] = "Empty"  # 소유권(order_id)은 유지, 상태만 초기화
                            logger.info("불량 발생으로 슬롯 %s-%s 복구 (오더 %s 전용)", f, c, item_info.order_id)
                            break

                    # 3. 불량품 배출(PA_DP) 태스크 추가
                    task_results.append(await self._create_result(item_info, TaskType.PA_DP))
                
                else:
                    # 양품일 경우 정상 적재(PA_GP) 진행
                    # (이후 Orchestrator에서 PA_GP 완료 시 'Occupied'로 변경 처리)
                    task_results.append(await self._create_result(item_info, TaskType.PA_GP))
                
                return task_results

        # [2] 상태 기반 흐름 제어 및 가드 (이벤트가 없을 때 실행)
        # 최초 투입 (신규 MM)
        if item_info.flow_stat == "CREATED":
            res = await self._create_result(item_info, TaskType.MM)
            res.priority = 0  # 신규 투입은 가장 낮은 우선순위
            task_results.append(res)
            return task_results

        # 특정 공정 완료 후 다음 이벤트 대기를 위해 흐름을 끊는 구간
        #stop_states = [TaskType.POUR,TaskType.PP, TaskType.INSP, TaskType.ToPAWait]
        #if item_info.last_task_type in stop_states:
        #    return []

        
        next_type = flow_map.get(item_info.last_task_type)
        if next_type:
            if isinstance(next_type, list):
                for t in next_type:
                    task_results.append(await self._create_result(item_info, t))
            else:
                task_results.append(await self._create_result(item_info, next_type))

        return task_results
    
    # txn 생성 to state manager
    async def _create_result(self, item_info: ItemStatusRecord, task_type: TaskType)-> NextTaskResult:  #현재 진행된 아이템 정보 , 다음 공정 이름
        # 트랜잭션 DB 기록 로직 (sm.insert_task_txn 호출 등) 포함
        item_info.strg_loc = await self._calculate_strg_loc(task_type, item_info) #
        curr_input = CreateTaskInput(item_id=item_info.item_id, task_type=task_type,  txn_stat=TxnStat.QUE , res_id =None )
        curr_txn_id = await self.sm.insert_task_txn(curr_input)

    
        return NextTaskResult(item_id=item_info.item_id, txn_id=curr_txn_id, task_type=task_type ) #proority = 0 
    
    #적재위치계산 
    async def _calculate_strg_loc(self, task_type: TaskType, item_info: ItemStatusRecord) -> str | None:
        if item_info.is_defective:
            return "DEFECTIVE_ZONE"
        
        if task_type == TaskType.ToPAWait:
            for (row, col), data in sorted(self.slot_table.items()):
                if data["order_id"] == item_info.order_id and data["status"] == "Reserved":
                    data["status"] = "Assigned"

                    strg_loc = f"{row}-{col}"

                    await self.sm.update_item_storage_location(
                        item_info.item_id,
                        strg_loc,
                    )

                    return strg_loc

        return item_info.strg_loc
    