from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

ROOT = Path(__file__).resolve().parents[1]
ORDER = "0123456789abcdef0123456789abcdef"
POLICY = {
    "currency": "BRL",
    "policy_version": "TEST_POLICY",
    "rules": {
        "canceled_order_paid": {
            "case_status": "action_required",
            "recommended_action": "issue_refund",
            "refund_brl": 79.0,
            "responsible_parties": [{"party_id": None, "party_type": "platform"}],
        },
        "late_delivery_seller": {
            "case_status": "action_required",
            "recommended_action": "refund_freight",
            "refund_brl": 18.0,
            "responsible_parties": [{"party_id": "seller-example", "party_type": "seller"}],
        },
        "valid_split_payment": {
            "case_status": "no_action",
            "recommended_action": "document_no_action",
            "refund_brl": 0.0,
            "responsible_parties": [{"party_id": None, "party_type": "customer"}],
        },
    },
}


def envelope(domain: str, data: Any, seq: int) -> dict[str, Any]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": f"ev_test_{domain}_{seq:04d}_abcdefghijkl",
        "result_hash": "sha256:" + "0" * 64,
        "domain": domain,
        "data": data,
        "warnings": [],
    }


def order(status: str, delivered: str | None, estimated: str, carrier: str) -> dict[str, Any]:
    return {
        "order_id": ORDER,
        "order_status": status,
        "order_purchase_timestamp": "2018-01-01T09:00:00-03:00",
        "order_approved_at": "2018-01-01T10:00:00-03:00",
        "order_delivered_carrier_date": carrier,
        "order_delivered_customer_date": delivered,
        "order_estimated_delivery_date": estimated,
    }


def item(limit: str, freight: str) -> dict[str, Any]:
    return {
        "order_id": ORDER,
        "order_item_id": "item-1",
        "seller_id": "seller-1",
        "shipping_limit_date": limit,
        "price": "79.00",
        "freight_value": freight,
    }


def capture(at: str, amount: str, kind: str = "captured") -> dict[str, Any]:
    return {"event_at": at, "event_type": kind, "amount_brl": amount, "status": "confirmed"}


class FakeGateway:
    def __init__(self, responses: dict[str, tuple[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    async def list_tools(self) -> list[str]:
        return sorted([*self.responses, "get_policy", "get_refund_timeline"])

    async def call(self, tool: str, *, case_id: str, **_: str) -> dict[str, Any]:
        self.calls.append((tool, case_id))
        if tool == "get_policy":
            return envelope("policy", POLICY, 99)
        if tool not in self.responses:
            raise RuntimeError(f"MCP tool {tool} failed: Error executing tool {tool}")
        domain, data = self.responses[tool]
        return envelope(domain, data, len(self.calls))


def run_case(tmp_path: Path, responses: dict[str, tuple[str, Any]], topic: str):
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)
    case = {
        "case_id": "TEST_CASE_001",
        "opened_at": "2018-01-20T09:00:00-03:00",
        "customer_request": {
            "claimed_order_id": ORDER,
            "claims": [
                {"claim_id": "claim-a", "topic": topic},
                {"claim_id": "claim-b", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "TEST_POLICY",
    }
    gateway = FakeGateway(responses)
    trace.emit(case_id=case["case_id"], event_type="case_received", actor="coordinator")
    output = asyncio.run(solve_case(case, gateway, trace))
    trace.emit(case_id=case["case_id"], event_type="case_finalized", actor="coordinator")
    contracts.validate_output(output, "output")
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    return output, events, gateway


def assert_workflow(output: dict[str, Any], events: list[dict[str, Any]]) -> None:
    kinds = {event["event_type"] for event in events}
    assert {"case_received", "task_assigned", "handoff", "policy_decided",
            "verification_completed", "case_finalized"} <= kinds
    assert events[0]["event_type"] == "case_received"
    assert events[-1]["event_type"] == "case_finalized"
    consumed = {
        ref for event in events if event["event_type"] == "tool_result_consumed"
        for ref in event["evidence_refs"]
    }
    assert set(output["evidence_refs"]) <= consumed
    assert len({event["actor"] for event in events}) >= 4


def test_canceled_paid_order_ignores_shifted_capture(tmp_path: Path) -> None:
    output, events, gateway = run_case(
        tmp_path,
        {
            "get_order": ("order", order("canceled", None, "2018-01-11T09:00:00-03:00",
                                         "2018-01-03T09:00:00-03:00")),
            "get_order_items": ("item", [item("2018-01-04T09:00:00-03:00", "10.00")]),
            "get_payment_timeline": ("payment", {"events": [
                capture("2018-01-01T10:00:00-03:00", "79.00"),
                capture("2018-05-01T10:00:00-03:00", "18.00"),
            ]}),
            "get_shipment_summary": ("shipment", {"events": []}),
        },
        "canceled_order_paid",
    )
    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["financial_resolution"]["recommended_refund_brl"] == pytest.approx(79.0)
    assert output["resolution_actions"] == ["issue_refund"]
    assert all(case_id == "TEST_CASE_001" for _, case_id in gateway.calls)
    assert_workflow(output, events)


def test_late_seller_handoff_attributes_order_seller(tmp_path: Path) -> None:
    output, events, _ = run_case(
        tmp_path,
        {
            "get_order": ("order", order("delivered", "2018-01-15T09:00:00-03:00",
                                         "2018-01-11T09:00:00-03:00",
                                         "2018-01-08T09:00:00-03:00")),
            "get_order_items": ("item", [
                item("2018-01-04T09:00:00-03:00", "18.00"),
                item("2017-10-04T09:00:00-03:00", "10.00"),
            ]),
            "get_sellers": ("seller", [{"seller_id": "seller-1"}]),
            "get_payment_timeline": ("payment", {"events": [
                capture("2018-01-01T10:00:00-03:00", "18.00"),
            ]}),
            "get_shipment_summary": ("shipment", {"events": []}),
        },
        "late_delivery_seller",
    )
    assert output["assessment"]["primary_issue"] == "late_delivery_seller"
    parties = output["root_cause_analysis"]["responsible_parties"]
    assert parties == [{"party_type": "seller", "party_id": "seller-1"}]
    assert output["affected_entities"]["seller_ids"] == ["seller-1"]
    assert output["financial_resolution"]["recommended_refund_brl"] == pytest.approx(18.0)
    assert_workflow(output, events)


def test_split_payment_matching_order_total_needs_no_action(tmp_path: Path) -> None:
    output, events, _ = run_case(
        tmp_path,
        {
            "get_order": ("order", order("delivered", "2018-01-08T09:00:00-03:00",
                                         "2018-01-11T09:00:00-03:00",
                                         "2018-01-03T09:00:00-03:00")),
            "get_order_items": ("item", [item("2018-01-04T09:00:00-03:00", "10.00")]),
            "get_payment_timeline": ("payment", {"events": [
                capture("2018-01-01T10:00:00-03:00", "44.50"),
                capture("2018-01-01T11:00:00-03:00", "44.50"),
            ]}),
            "get_shipment_summary": ("shipment", {"events": []}),
        },
        "duplicate_charge",
    )
    assessment = output["assessment"]
    assert assessment["primary_issue"] == "valid_split_payment"
    assert assessment["case_status"] == "no_action"
    assert output["financial_resolution"] == {
        "currency": "BRL", "recommended_refund_brl": 0.0, "refund_lines": []
    }
    assert assessment["confidence"] < 0.9
    assert_workflow(output, events)


def test_missing_order_is_insufficient_evidence(tmp_path: Path) -> None:
    output, events, _ = run_case(tmp_path, {}, "canceled_order_paid")
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["affected_entities"]["order_ids"] == []
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    assert_workflow(output, events)
