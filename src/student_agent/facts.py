"""Normalise MCP evidence payloads into typed facts the decision engine can reason over.

MCP payloads contain rows that belong to other timelines (shifted decoy rows) and exact
duplicate rows. Facts keep only rows anchored to this order's own timeline and record
what was excluded so the decision can report it as a data conflict. Nothing is invented:
an absent field stays ``None`` and is treated as unknown.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

MONEY_TOLERANCE = 0.01
CAPTURE_WINDOW = timedelta(hours=24)
DEFAULT_SHIPPING_WINDOW = timedelta(days=10)


def as_rows(data: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = next((data[key] for key in keys if isinstance(data.get(key), list)), [])
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def unique_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Drop byte-identical replicated rows; return kept rows and the number dropped."""
    seen: set[str] = set()
    kept: list[dict[str, Any]] = []
    for row in rows:
        key = json.dumps(row, sort_keys=True)
        if key not in seen:
            seen.add(key)
            kept.append(row)
    return kept, len(rows) - len(kept)


def money(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def same_amount(left: float | None, right: float | None) -> bool:
    return left is not None and right is not None and abs(left - right) <= MONEY_TOLERANCE


@dataclass(frozen=True)
class ItemFact:
    item_id: str
    seller_id: str | None
    price: float | None
    freight: float | None
    shipping_limit_at: datetime | None


@dataclass(frozen=True)
class MoneyEvent:
    kind: str
    status: str | None
    amount: float | None
    at: datetime | None


@dataclass
class Exclusion:
    """A row that conflicts with the order's own timeline and was not used."""

    field: str
    sources: tuple[str, ...]
    selected_source: str | None
    resolution_code: str


@dataclass
class CaseFacts:
    order_id: str
    opened_at: datetime | None = None
    order_found: bool = False
    order_status: str | None = None
    purchase_at: datetime | None = None
    approved_at: datetime | None = None
    carrier_at: datetime | None = None
    delivered_at: datetime | None = None
    estimated_at: datetime | None = None
    items: list[ItemFact] = field(default_factory=list)
    seller_ids: list[str] = field(default_factory=list)
    captures: list[MoneyEvent] = field(default_factory=list)
    payment_flags: list[MoneyEvent] = field(default_factory=list)
    refunds: list[MoneyEvent] = field(default_factory=list)
    shipment_late_actor: str | None = None
    policy_rules: dict[str, dict[str, Any]] = field(default_factory=dict)
    exclusions: list[Exclusion] = field(default_factory=list)
    shipment_disagrees: list[str] = field(default_factory=list)

    @property
    def item_ids(self) -> list[str]:
        return list(dict.fromkeys(item.item_id for item in self.items))

    @property
    def order_total(self) -> float | None:
        if not self.items or any(item.price is None for item in self.items):
            return None
        return round(sum((item.price or 0.0) + (item.freight or 0.0) for item in self.items), 2)

    @property
    def freight_total(self) -> float | None:
        if not self.items:
            return None
        return round(sum(item.freight or 0.0 for item in self.items), 2)

    @property
    def captured_total(self) -> float:
        return round(sum(event.amount or 0.0 for event in self.captures), 2)

    @property
    def shipping_limit_at(self) -> datetime | None:
        limits = [item.shipping_limit_at for item in self.items if item.shipping_limit_at]
        return max(limits) if limits else None

    @property
    def is_late(self) -> bool | None:
        if self.delivered_at is None or self.estimated_at is None:
            return None
        return self.delivered_at > self.estimated_at

    @property
    def seller_handoff_late(self) -> bool | None:
        limit = self.shipping_limit_at
        if self.carrier_at is None or limit is None:
            return None
        return self.carrier_at > limit


def _event(row: dict[str, Any]) -> MoneyEvent:
    status = text(row.get("status"))
    return MoneyEvent(
        (text(row.get("event_type")) or "unknown").lower(),
        status.lower() if status else None,
        money(row.get("amount_brl", row.get("payment_value"))),
        timestamp(row.get("event_at")),
    )


def apply_order(facts: CaseFacts, data: Any) -> None:
    if not isinstance(data, dict) or not data.get("order_id"):
        return
    facts.order_found = True
    status = text(data.get("order_status"))
    facts.order_status = status.lower() if status else None
    facts.purchase_at = timestamp(data.get("order_purchase_timestamp"))
    facts.approved_at = timestamp(data.get("order_approved_at")) or facts.purchase_at
    facts.carrier_at = timestamp(data.get("order_delivered_carrier_date"))
    facts.delivered_at = timestamp(data.get("order_delivered_customer_date"))
    facts.estimated_at = timestamp(data.get("order_estimated_delivery_date"))


def _shipping_window(facts: CaseFacts) -> tuple[datetime | None, datetime | None]:
    if facts.purchase_at is None:
        return None, None
    upper = facts.estimated_at or facts.purchase_at + DEFAULT_SHIPPING_WINDOW
    return facts.purchase_at, upper


def apply_items(facts: CaseFacts, data: Any) -> None:
    rows, replicated = unique_rows(as_rows(data, "items"))
    if replicated:
        facts.exclusions.append(
            Exclusion("order_items.row", ("order_items.row_1", "order_items.row_2"),
                      "order_items.row_1", "DUPLICATE_ROW_DEDUPLICATED")
        )
    lower, upper = _shipping_window(facts)
    by_item: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        item_id = text(row.get("order_item_id"))
        if item_id and text(row.get("order_id")) in (None, facts.order_id):
            by_item.setdefault(item_id, []).append(row)
    for item_id, candidates in by_item.items():
        def anchored(row: dict[str, Any]) -> bool:
            limit = timestamp(row.get("shipping_limit_date"))
            return bool(limit and lower and upper and lower <= limit <= upper)

        chosen = [row for row in candidates if anchored(row)]
        if not chosen and lower is not None:
            dated = [row for row in candidates if timestamp(row.get("shipping_limit_date"))]
            dated.sort(key=lambda r: abs(timestamp(r["shipping_limit_date"]) - lower))
            chosen = dated[:1]
        chosen = sorted(chosen, key=lambda r: timestamp(r.get("shipping_limit_date")) or lower)
        selected = chosen[:1] or candidates[:1]
        if len(candidates) > 1:
            facts.exclusions.append(
                Exclusion("order_items.shipping_limit_date",
                          ("order_items.anchored_row", "order_items.shifted_row"),
                          "order_items.anchored_row", "OUT_OF_ORDER_TIMELINE_EXCLUDED")
            )
        for row in selected:
            facts.items.append(
                ItemFact(item_id, text(row.get("seller_id")), money(row.get("price")),
                         money(row.get("freight_value")),
                         timestamp(row.get("shipping_limit_date")))
            )
    for item in facts.items:
        if item.seller_id and item.seller_id not in facts.seller_ids:
            facts.seller_ids.append(item.seller_id)


def apply_sellers(facts: CaseFacts, data: Any) -> None:
    known = {row.get("seller_id") for row in as_rows(data, "sellers")}
    facts.seller_ids = [seller for seller in facts.seller_ids if not known or seller in known]


def apply_payment_timeline(facts: CaseFacts, data: Any) -> None:
    rows, replicated = unique_rows(as_rows(data, "events"))
    anchor = facts.approved_at
    shifted = 0
    for row in rows:
        event = _event(row)
        anchored = bool(
            anchor
            and event.at
            and anchor - timedelta(hours=1) <= event.at <= anchor + CAPTURE_WINDOW
        )
        if not anchored:
            shifted += 1
            continue
        if event.kind == "captured" and event.status in (None, "confirmed", "succeeded"):
            facts.captures.append(event)
        elif event.kind != "captured":
            facts.payment_flags.append(event)
    if replicated:
        facts.exclusions.append(
            Exclusion("payment_timeline.events", ("payment_timeline.event_1",
                      "payment_timeline.event_2"), "payment_timeline.event_1",
                      "DUPLICATE_ROW_DEDUPLICATED")
        )
    if shifted:
        facts.exclusions.append(
            Exclusion("payment_timeline.captured_at", ("payment_timeline.approval_window",
                      "payment_timeline.shifted_events"), "payment_timeline.approval_window",
                      "OUT_OF_ORDER_TIMELINE_EXCLUDED")
        )


def apply_refund_timeline(facts: CaseFacts, data: Any) -> None:
    lower, upper = facts.purchase_at, facts.opened_at
    shifted = 0
    for row in unique_rows(as_rows(data, "events"))[0]:
        event = _event(row)
        if lower and upper and event.at and lower <= event.at <= upper:
            facts.refunds.append(event)
        else:
            shifted += 1
    if shifted:
        facts.exclusions.append(
            Exclusion("refund_timeline.event_at", ("refund_timeline.case_window",
                      "refund_timeline.shifted_events"), "refund_timeline.case_window",
                      "OUT_OF_CASE_WINDOW_EXCLUDED")
        )


def apply_shipment(facts: CaseFacts, data: Any) -> None:
    if not isinstance(data, dict):
        return
    pairs = {
        "delivered_carrier_at": facts.carrier_at,
        "delivered_customer_at": facts.delivered_at,
        "estimated_delivery_at": facts.estimated_at,
    }
    for key, order_value in pairs.items():
        if key in data and timestamp(data.get(key)) != order_value:
            facts.shipment_disagrees.append(key)
    for row in as_rows(data, "events"):
        event_at = timestamp(row.get("event_at"))
        if (
            text(row.get("event_type")) == "delivered_late"
            and facts.delivered_at is not None
            and event_at == facts.delivered_at
        ):
            facts.shipment_late_actor = text(row.get("actor"))
        elif text(row.get("event_type")) == "delivered_late":
            facts.exclusions.append(
                Exclusion("shipment.delivered_late", ("order.delivery_timestamps",
                          "shipment.events"), "order.delivery_timestamps",
                          "AUTHORITATIVE_ORDER_TIMESTAMPS_SELECTED")
            )


def apply_policy(facts: CaseFacts, data: Any) -> None:
    rules = data.get("rules") if isinstance(data, dict) else None
    if isinstance(rules, dict):
        facts.policy_rules = {key: value for key, value in rules.items() if isinstance(value, dict)}
