from __future__ import annotations

from datetime import datetime
from typing import Any

from . import facts as F
from .a2a import A2ABus
from .agents import (
    COORDINATOR,
    CaseContext,
    OrderAgent,
    PaymentAgent,
    PolicyAgent,
    ShipmentAgent,
)
from .decision import SELLER_ISSUES
from .evidence import EvidenceLedger
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter
from .verifier import VERIFIER, build_output, verify


async def _discovered_tools(gateway: EvidenceGateway) -> frozenset[str]:
    cached = getattr(gateway, "_student_discovered_tools", None)
    if cached is None:
        cached = frozenset(await gateway.list_tools())
        gateway._student_discovered_tools = cached  # type: ignore[attr-defined]
    return cached


def _opened_at(case: dict[str, Any]) -> datetime | None:
    return F.timestamp(case.get("opened_at"))


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinator: dispatch specialists over A2A, decide via policy agent, verify, finalize.

    The customer message is never treated as ground truth; only MCP evidence is.
    """
    case_id = case["case_id"]
    request = case.get("customer_request") or {}
    order_id = str(request.get("claimed_order_id") or "").strip()
    claims = [c for c in request.get("claims") or [] if c.get("claim_id") and c.get("topic")]
    ctx = CaseContext(
        case_id=case_id,
        order_id=order_id,
        policy_version=str(case.get("policy_version") or ""),
        claims=claims,
        gateway=gateway,
        trace=trace,
        bus=A2ABus(case_id, trace),
        ledger=EvidenceLedger(case_id),
        discovered=await _discovered_tools(gateway),
        facts=F.CaseFacts(order_id=order_id, opened_at=_opened_at(case)),
    )
    bus = ctx.bus
    order_agent = OrderAgent()

    if order_id:
        bus.assign(COORDINATOR, order_agent.actor, "LOAD_ORDER_SCOPE", order_id=order_id)
        await order_agent.load_scope(ctx)

    if ctx.facts.order_found:
        payment_agent, shipment_agent = PaymentAgent(), ShipmentAgent()
        bus.assign(COORDINATOR, payment_agent.actor, "RECONCILE_PAYMENTS")
        await payment_agent.reconcile(ctx)
        bus.assign(COORDINATOR, shipment_agent.actor, "ASSESS_DELIVERY")
        await shipment_agent.assess(ctx)

    policy_agent = PolicyAgent()
    bus.assign(COORDINATOR, policy_agent.actor, "APPLY_POLICY", policy=ctx.policy_version)
    decision = await policy_agent.decide(ctx)

    if decision.primary_issue in SELLER_ISSUES and ctx.facts.order_found:
        bus.assign(COORDINATOR, order_agent.actor, "CONFIRM_RESPONSIBLE_SELLER")
        await order_agent.confirm_sellers(ctx)

    draft = build_output(ctx, decision)
    bus.handoff(COORDINATOR, VERIFIER, "VERIFY_DRAFT", draft["evidence_refs"])
    output, failed = verify(ctx, draft)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor=VERIFIER,
        decision_code="PASS" if not failed else "REPAIRED",
        evidence_refs=output["evidence_refs"][:20] or None,
        attributes={
            "failed_checks": ",".join(failed)[:80] or None,
            "tool_failures": len(ctx.ledger.failures),
            "primary_issue": decision.primary_issue,
        },
    )
    bus.handoff(VERIFIER, COORDINATOR, "READY_TO_FINALIZE", output["evidence_refs"])
    return output
