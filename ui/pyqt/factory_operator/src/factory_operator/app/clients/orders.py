"""Orders 도메인 mixin — 발주/최근주문 조회."""

from __future__ import annotations

from typing import Any

from app import mock_data


class OrdersMixin:
    """발주 조회 endpoints."""

    # ===== smartcast schema =====
    def get_smartcast_orders(self) -> list[dict[str, Any]] | None:
        """모든 발주 (관리자용)."""
        return self._get("/api/orders", mock_value=mock_data.RECENT_ORDERS)

    def lookup_orders_by_email(self, email: str) -> list[dict[str, Any]] | None:
        """Pink GUI #1 — 이메일로 발주 조회 (없으면 빈 배열)."""
        from urllib.parse import quote

        return self._get(f"/api/orders/lookup?email={quote(email)}", mock_value=[])

    def get_recent_orders(self) -> list[dict[str, Any]]:
        data = self._get("/api/orders", mock_value=mock_data.RECENT_ORDERS)
        if isinstance(data, list) and data:
            normalized: list[dict[str, Any]] = []
            for item in data[:10]:
                normalized.append(
                    {
                        "id": item.get("id", item.get("order_number", "")),
                        "customer": item.get("customer", item.get("customer_name", "")),
                        "amount": item.get("amount", item.get("total_price", 0)),
                        "due_date": item.get("due_date", item.get("delivery_date", "")),
                        "status": item.get("status", ""),
                    }
                )
            return normalized
        return mock_data.RECENT_ORDERS
