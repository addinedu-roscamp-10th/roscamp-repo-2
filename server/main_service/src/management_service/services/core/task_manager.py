"""Compatibility shell for the deprecated TaskManager dependency."""

from __future__ import annotations

import logging
from typing import List
from services.contracts.models import *
from services.contracts.protocols import ITaskManager , IStateManager

logger = logging.getLogger(__name__)


# ============================================================
# 작업 우선순위표
# ------------------------------------------------------------
# 15점 : 배터리 저하로 인한 ToPP 작업 재할당
# 10점 : 배터리 저하 AMR 충전소 이동 작업(ToCHG)
# 10점 : 불량으로 인한 재생산 작업(MM)
#  7점 : 적재 구역으로 이동 (ToSTRG) 
#  5점 : 일반 생산 공정 작업(MM,ToSTRG 제외)
#  3점 : 출고 관련 작업(PICK, ToSHIP)
#  0점 : 신규 주문 최초 작업(MM)
# ============================================================

class TaskManager(ITaskManager):
    MAX_COL = 6
    MAX_ROW = 3
    SHIP_BATCH_SIZE = 3 
    
    def __init__(self, sm:IStateManager):
        self.sm = sm
        self.slot_table: dict[tuple[int, int], dict] = {}  # 메모리 상의 보관랙

        self._init_slot_table()
        self.ship_plans: dict[int, dict] = {}  #order_id별 출고 진행 상태
 

    def _init_slot_table(self):
        """3x6 적재 슬롯 초기화"""
        for row in range(1, self.MAX_ROW + 1):
            for col in range(1, self.MAX_COL + 1):
                self.slot_table[(row, col)] = {
                    "status": "empty",
                    "order_id": None,
                }

        # 임시 점유 테스트용 1-1~1-3
        # self.slot_table[(1, 1)]["status"] = "occupied"
        # self.slot_table[(1, 2)]["status"] = "occupied"
        # self.slot_table[(1, 3)]["status"] = "occupied"


    #오더 투입시 해당 오더가 사용할 적재 공간 예약
    async def reserve_rack_slots(self, order_id: int, start_pos: tuple[int, int], target_qty: int) -> int:
        """주문 투입 시 해당 오더가 사용할 슬롯들을 가상 랙에 예약한다."""
        row, col = start_pos

        assigned = 0
        candidate_positions: list[tuple[int, int]] = []
        current_abs_idx = (row - 1) * self.MAX_COL + (col - 1)

        while assigned < target_qty:
            curr_row = (current_abs_idx // self.MAX_COL) + 1
            curr_col = (current_abs_idx % self.MAX_COL) + 1

            pos_key = (curr_row, curr_col)

            if pos_key not in self.slot_table:
                logger.warning("랙 공간 부족: %s-%s 위치가 slot_table에 없음", curr_row, curr_col)
                break

            slot = self.slot_table[pos_key]

            if slot.get("status") != "empty" or slot.get("order_id") is not None:
                logger.warning("예약 불가 슬롯: %s-%s slot=%s", curr_row, curr_col, slot)
                break

            assigned += 1
            candidate_positions.append(pos_key)
            current_abs_idx += 1

        logger.info("주문 %s 구역 예약 완료: start=%s target=%s assigned=%s",
                    order_id, start_pos, target_qty, assigned)

        if assigned != target_qty:
            return assigned

        if assigned == 0:
            return 0

        reserved_count = await self.sm.reserve_storage_slots(start_pos, assigned)
        if reserved_count != assigned:
            logger.warning(
                "DB 적재 슬롯 예약 불일치: order_id=%s start=%s requested=%s reserved=%s",
                order_id,
                start_pos,
                assigned,
                reserved_count,
            )
            return int(reserved_count or 0)

        # DB 예약 성공 시 메모리 반영
        for position in candidate_positions:
            self.slot_table[position] = {
                "status": "reserved",
                "order_id": order_id,
            }

        return assigned

    def log_slot_table(self):
        """현재 slot_table 상태를 로그 출력"""

        logger.info("============= SLOT TABLE =============")

        for row in range(1, self.MAX_ROW + 1):
            line = []

            for col in range(1, self.MAX_COL + 1):
                slot = self.slot_table[(row, col)]

                status = slot["status"]
                order_id = slot["order_id"]

                if status == "empty":
                    text = f"{row}-{col}:EMPTY"
                else:
                    text = f"{row}-{col}:{status}(O{order_id})"

                line.append(f"{text:25}")

            logger.info(" ".join(line))

        logger.info("======================================")

    def release_shipped_slots(self, batch: list[tuple[int, int, int]]) -> None:
        """출고된 slot을 메모리에서도 해제."""
        for _item_id, row, col in batch:
            position = (int(row), int(col))
            if position not in self.slot_table:
                logger.warning("출고 슬롯이 slot_table에 없음: position=%s", position)
                continue
            self.slot_table[position] = {
                "status": "empty",
                "order_id": None,
            }
        logger.info("출고 슬롯 메모리 해제 완료: batch=%s", batch)

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
            
            elif event == "amr_battery_low_ToPP": #tochg = 10 topp =15
                task_results.append(NextTaskResult(
                        item_id=item_info.item_id, 
                        txn_id=await self.sm.insert_task_txn(CreateTaskInput(item_id=item_info.item_id, task_type=TaskType.ToPP )),
                        task_type=TaskType.ToPP, 
                        priority=15
                    ))
                return task_results
            
            elif event == "amr_battery_low_ToCHG": #tochg = 10  topp =15   주차자리 정하기
                chg_loc = await self.sm.get_empty_charger(item_info.req_res_id) ####사용가능한 주차 자리 1개 반환 및 예약
                if chg_loc is None:
                    logger.warning(
                        "charger 예약 실패: item_id=%s res_id=%s",
                        item_info.item_id,
                        item_info.req_res_id,
                    )
                task_results.append(NextTaskResult(
                        item_id=item_info.item_id, 
                        txn_id=await self.sm.insert_task_txn(CreateTaskInput(item_id=item_info.item_id, task_type=TaskType.ToCHG,chg_loc=chg_loc)),
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
                        item_id=replacement_id,
                        txn_id=await self.sm.insert_task_txn(CreateTaskInput(item_id=replacement_id, task_type=TaskType.MM)),
                        task_type=TaskType.MM, 
                        priority=10
                    ))


                    # 3. 불량품 배출(PA_DP) 태스크 추가
                    task_results.append(await self._create_result(item_info, TaskType.PA_DP))
                
                else:
                    # 양품일 경우 정상 적재(PA_GP) 진행
                    # Occupied 확정은 Executor가 PA_GP 성공 완료 시 State manager가 반영
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
                    res = await self._create_result(item_info, t)

                    if t == TaskType.ToSTRG:
                        res.priority = 7
                    task_results.append(res)
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
    
    # 적재위치계산
    async def _calculate_strg_loc(self, task_type: TaskType, item_info: ItemStatusRecord) -> str | None:
        """불량품은 불량 구역을, 양품은 주문에 예약된 슬롯을 적재 위치로 선택한다."""
        if item_info.is_defective:
            return "DEFECTIVE_ZONE"
        
        if task_type == TaskType.ToPAWait:
            for (row, col), data in sorted(self.slot_table.items()):
                if data["order_id"] == item_info.order_id and data["status"] == "reserved":
                    strg_loc = (row, col)

                    await self.sm.update_item_storage_location(
                        item_info.item_id,
                        strg_loc,
                    )

                    # 양품으로 할당된 슬롯을 메모리에서 점유 처리
                    data["status"] = "occupied"

                    return strg_loc

        return item_info.strg_loc
    

    #출고 객체 생성
    async def create_ship_task(self, order_id: int, item_locations: list[tuple[int, int, int]], event: str = None) -> ShipTaskResult | None:
        """
        출고 전용 task 생성 함수.

        event:
        - None 또는 "ship_start"       : 첫 ToSHIP 생성
        - "toship_src_arrived"         : 현재 batch PICK 생성
        - "pick_done"                 : 현재 batch 제거 후 다음 ToSHIP 생성
        """

        # 최초 출고 시작 시에만 batch 생성
        if order_id not in self.ship_plans:
            if not item_locations:
                logger.warning("출고 시작 실패: item_locations 없음 order_id=%s", order_id)
                return None

            self.ship_plans[order_id] = {  # 주문별 batches(아이템 3개씩 끊은 값) 
                "batches": [
                    item_locations[i:i + self.SHIP_BATCH_SIZE]
                    for i in range(0, len(item_locations), self.SHIP_BATCH_SIZE)
                ]
            }

            logger.info(  "출고 계획 생성: order_id=%s batches=%s",  order_id,  self.ship_plans[order_id]["batches"], )

        plan = self.ship_plans.get(order_id)

        if not plan or not plan["batches"]:
            logger.warning("출고 계획 없음 또는 남은 batch 없음: order_id=%s", order_id)
            self.ship_plans.pop(order_id, None)
            return None

        # 출고 시작 → 현재 batch ToSHIP 생성
        if event is None or event == "ship_start":
            return await self._create_ship_task(
                order_id=order_id,
                task_type=TaskType.ToSHIP,
            )
        # ToSHIP 출발지 도착 → 현재 batch PICK 생성
        elif event == "toship_src_arrived":
            return await self._create_ship_task(
                order_id=order_id,
                task_type=TaskType.PICK,
            )
        # PICK 완료 → 현재 batch 제거 후 다음 batch ToSHIP 생성
        elif event == "pick_done":
            #finished_batch = plan["batches"].pop(0)

            #logger.info( "출고 batch 완료: order_id=%s finished_batch=%s", order_id, finished_batch, )

            if not plan["batches"]:
                logger.info("출고 작업 전체 완료: order_id=%s", order_id)
                del self.ship_plans[order_id]
                return None

            return await self._create_ship_task(
                order_id=order_id,
                task_type=TaskType.ToSHIP,
            )

        else:
            logger.warning("알 수 없는 출고 이벤트: order_id=%s event=%s", order_id, event)
            return None
        
    def pop_finished_ship_batch(self, order_id: int) -> list[tuple[int, int, int]]: 
        plan = self.ship_plans.get(order_id)  

        if not plan or not plan["batches"]:  
            logger.warning("완료 처리할 출고 batch 없음: order_id=%s", order_id)  
            self.ship_plans.pop(order_id, None)  
            return []  

        finished_batch = plan["batches"].pop(0)  

        logger.info(  
            "출고 batch 완료: order_id=%s finished_batch=%s",  
            order_id,  
            finished_batch,  
        )  

        if not plan["batches"]:  
            logger.info("출고 작업 전체 완료: order_id=%s", order_id)  
            del self.ship_plans[order_id] 

        return finished_batch  

    async def _create_ship_task( self, order_id: int, task_type: TaskType, ) -> ShipTaskResult | None:
        """
        현재 batch 기준으로 ToSHIP 또는 PICK task 생성.

        - ToSHIP: AMR 이동 작업이므로 batch 반환 안 함
        - PICK: 로봇팔 작업이므로 batch 반환
        """

        plan = self.ship_plans.get(order_id)
        current_batch = plan["batches"][0]

        # 현재 batch의 첫 번째 item을 대표 item으로 사용
        representative_item_id = current_batch[0][0]

        txn_id = await self.sm.insert_task_txn(
            CreateTaskInput(
                item_id=representative_item_id,
                task_type=task_type,
                txn_stat=TxnStat.QUE,
                res_id=None,
            )
        )

        logger.info( "출고 task 생성: order_id=%s task_type=%s batch=%s", order_id, task_type,  current_batch, )

        if task_type == TaskType.PICK:
            return ShipTaskResult(
                txn_id=txn_id,
                task_type=task_type,
                priority=3,
                batch=current_batch,
            )

        return ShipTaskResult(
            txn_id=txn_id,
            task_type=task_type,
            priority=3,
            batch=current_batch,
        )
    
    def has_ship_plan(self, order_id: int) -> bool:
        plan = self.ship_plans.get(order_id)

        if not plan:
            return False

        return bool(plan.get("batches"))
