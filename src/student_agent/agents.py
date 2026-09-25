"""Specialist agents. Each actor owns a narrow tool allow-list and reports via A2A handoff."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import facts as F
from .a2a import A2ABus
from .decision import Decision, decide
from .evidence import EvidenceLedger, ToolRunner
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

COORDINATOR = "coordinator"

TOOL_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "order-agent": ("get_order", "get_order_items", "get_sellers"),
    "payment-agent": ("get_payment_timeline", "get_refund_timeline"),
    "shipment-agent": ("get_shipment_summary",),
    "policy-agent": ("get_policy",),
    "verifier": (),
}


@dataclass
class CaseContext:
    case_id: str
    order_id: str
    policy_version: str
    claims: list[dict[str, Any]]
    gateway: EvidenceGateway
    trace: TraceWriter
    bus: A2ABus
    ledger: EvidenceLedger
    discovered: frozenset[str]
    facts: F.CaseFacts

    @property
    def claimed_topics(self) -> list[str]:
        return [claim["topic"] for claim in self.claims]

    def runner(self, actor: str) -> ToolRunner:
        return ToolRunner(
            actor, TOOL_PERMISSIONS[actor], self.gateway, self.trace, self.ledger, self.discovered
        )


def _refs(ctx: CaseContext, *tools: str) -> list[str]:
    return [ref for tool in tools if (ref := ctx.ledger.ref(tool))]


class OrderAgent:
    actor = "order-agent"

    async def load_scope(self, ctx: CaseContext) -> None:
        runner = ctx.runner(self.actor)
        order = await runner.fetch("get_order", order_id=ctx.order_id)
        if order is not None:
            F.apply_order(ctx.facts, order.data)
        if ctx.facts.order_found:
            items = await runner.fetch("get_order_items", order_id=ctx.order_id)
            if items is not None:
                F.apply_items(ctx.facts, items.data)
        ctx.bus.handoff(
            self.actor,
            COORDINATOR,
            "ORDER_SCOPE_READY" if ctx.facts.order_found else "ORDER_NOT_FOUND",
            _refs(ctx, "get_order", "get_order_items"),
        )

    async def confirm_sellers(self, ctx: CaseContext) -> None:
        sellers = await ctx.runner(self.actor).fetch("get_sellers", order_id=ctx.order_id)
        if sellers is not None:
            F.apply_sellers(ctx.facts, sellers.data)
        ctx.bus.handoff(
            self.actor,
            COORDINATOR,
            "SELLER_CONFIRMED" if sellers is not None else "SELLER_UNCONFIRMED",
            _refs(ctx, "get_sellers"),
        )


class PaymentAgent:
    actor = "payment-agent"

    async def reconcile(self, ctx: CaseContext) -> None:
        runner = ctx.runner(self.actor)
        timeline = await runner.fetch("get_payment_timeline", order_id=ctx.order_id)
        if timeline is not None:
            F.apply_payment_timeline(ctx.facts, timeline.data)
        refunds = await runner.fetch("get_refund_timeline", order_id=ctx.order_id)
        if refunds is not None:
            F.apply_refund_timeline(ctx.facts, refunds.data)
        ctx.bus.handoff(
            self.actor,
            COORDINATOR,
            "PAYMENT_RECONCILED" if timeline is not None else "PAYMENT_UNAVAILABLE",
            _refs(ctx, "get_payment_timeline", "get_refund_timeline"),
        )


class ShipmentAgent:
    actor = "shipment-agent"

    async def assess(self, ctx: CaseContext) -> None:
        summary = await ctx.runner(self.actor).fetch("get_shipment_summary", order_id=ctx.order_id)
        if summary is not None:
            F.apply_shipment(ctx.facts, summary.data)
        if summary is None:
            code = "SHIPMENT_UNAVAILABLE"
        elif ctx.facts.is_late:
            code = "DELIVERY_LATE"
        else:
            code = "DELIVERY_NOT_LATE"
        ctx.bus.handoff(self.actor, COORDINATOR, code, _refs(ctx, "get_shipment_summary"))


class PolicyAgent:
    actor = "policy-agent"

    async def decide(self, ctx: CaseContext) -> Decision:
        policy = await ctx.runner(self.actor).fetch(
            "get_policy", policy_version=ctx.policy_version
        )
        if policy is not None:
            F.apply_policy(ctx.facts, policy.data)
        decision = decide(ctx.facts, ctx.claimed_topics)
        ctx.trace.emit(
            case_id=ctx.case_id,
            event_type="policy_decided",
            actor=self.actor,
            decision_code=decision.primary_issue,
            evidence_refs=_refs(ctx, *decision.citations) or None,
            attributes={
                "case_status": decision.case_status,
                "recommended_action": decision.recommended_action,
                "refund_brl": decision.refund_brl,
                "confidence": decision.confidence,
            },
        )
        ctx.bus.handoff(
            self.actor, COORDINATOR, "POLICY_DECIDED", _refs(ctx, "get_policy")
        )
        return decision
