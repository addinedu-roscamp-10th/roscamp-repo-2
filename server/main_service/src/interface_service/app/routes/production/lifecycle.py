from __future__ import annotations
from fastapi import APIRouter, HTTPException
from app.clients.management import ManagementClient, ManagementUnavailable
from app.schemas.schemas import ShippingStartRequest

router = APIRouter(prefix="/api/production", tags=["production"])


# -----------------------------------------------------------------------------
# Shipping Start
# 적재 완료된 주문 (ord_stat=SHIP) 의 TAT 출하 task 를 trigger.
# Web /orders 화면의 "출하 시작" 버튼 또는 PyQt 적재완료 화면이 호출한다.
# -----------------------------------------------------------------------------

@router.post("/shipping/start")
def start_shipping(payload: ShippingStartRequest) -> dict:
    """orchestrator.start_shipping 위임 — TAT 가 적재장 → 출하장 운반을 시작한다.

    Returns: {ord_id, item_ids, accepted, message}
    503: Management gRPC 미가동. 적재 task 는 Management 가 단독 소유하므로 local fallback 없음.
    """
    if payload.ord_id <= 0:
        raise HTTPException(400, "ord_id must be a positive integer")
    try:
        return ManagementClient.get().start_shipping(payload.ord_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ManagementUnavailable as exc:
        raise HTTPException(503, f"Management Service unavailable: {exc}") from exc
