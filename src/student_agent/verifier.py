"""Output assembly and pre-finalize verification invariants."""

from __future__ import annotations

from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .agents import CaseContext
from .contracts import ContractError
from .decision import Decision
from .facts import same_amount

VERIFIER = "verifier"
FULL_REFUND_ISSUES = {"canceled_order_paid", "unavailable_order_paid", "refund_failed"}


def _claim_verdict(topic: str, decision: Decision) -> str:
    issue = decision.primary_issue
    if topic == "requested_full_refund":
        if issue in FULL_REFUND_ISSUES and decision.refund_brl > 0:
            return "supported"
        if decision.case_status == "needs_investigation":
            return "insufficient_evidence"
        return "partially_supported" if decision.refund_brl > 0 else "unsupported"
    if issue == "insufficient_evidence":
        return "insufficient_evidence"
    if topic == issue:
        return "unsupported" if topic == "unsupported_claim" else "supported"
    return "unsupported"


def build_output(ctx: CaseContext, decision: Decision) -> dict[str, Any]:
    facts = ctx.facts
    cited = [ref for tool in decision.citations if (ref := ctx.ledger.ref(tool))]
    refund = decision.refund_brl
    order_ids = [ctx.order_id] if facts.order_found else []
    conflicts: list[dict[str, Any]] = []
    for exclusion in facts.exclusions:
        if any(c["field"] == exclusion.field for c in conflicts) or len(conflicts) >= 5:
            continue
        conflicts.append(
            {
                "field": exclusion.field,
                "sources": list(exclusion.sources),
                "selected_source": exclusion.selected_source,
                "resolution_code": exclusion.resolution_code,
            }
        )
    claims = []
    for claim in ctx.claims[:5]:
        verdict = _claim_verdict(claim["topic"], decision)
        confidence = decision.confidence if verdict != "insufficient_evidence" else 0.5
        claims.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": cited,
            }
        )
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": ctx.case_id,
        "assessment": {
            "primary_issue": decision.primary_issue,
            "case_status": decision.case_status,
            "confidence": decision.confidence,
        },
        "affected_entities": {
            "order_ids": order_ids,
            "item_ids": facts.item_ids[:20],
            "seller_ids": facts.seller_ids[:20],
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": claims,
        "root_cause_analysis": {
            "ranked_causes": [
                {"cause_code": code, "rank": rank}
                for rank, code in enumerate(decision.cause_codes[:5], 1)
            ],
            "responsible_parties": decision.responsible_parties,
        },
        "evidence_refs": cited,
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund,
            "refund_lines": (
                [
                    {
                        "reason_code": decision.recommended_action,
                        "amount_brl": refund,
                        "entity_id": ctx.order_id,
                    }
                ]
                if refund > 0
                else []
            ),
        },
        "resolution_actions": [decision.recommended_action],
    }


def verify(ctx: CaseContext, output: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Check invariants; repair what is mechanically repairable. Returns failed checks."""
    failed: list[str] = []
    refs = output["evidence_refs"]
    owned = [ref for ref in refs if ctx.ledger.owns(ref)]
    if owned != refs:
        failed.append("EVIDENCE_OWNERSHIP")
        output["evidence_refs"] = owned
        for claim in output.get("claim_assessments", []):
            claim["evidence_refs"] = [r for r in claim["evidence_refs"] if ctx.ledger.owns(r)]
    if ctx.facts.order_found and ctx.ledger.ref("get_order") not in output["evidence_refs"]:
        failed.append("ORDER_EVIDENCE_MISSING")

    entities = output["affected_entities"]
    if any(order != ctx.order_id for order in entities["order_ids"]):
        failed.append("ENTITY_SCOPE")
        entities["order_ids"] = [ctx.order_id]
    sellers = set(entities["seller_ids"])
    for party in output["root_cause_analysis"]["responsible_parties"]:
        if party["party_type"] == "seller" and party["party_id"] not in sellers:
            failed.append("SELLER_NOT_IN_SCOPE")

    money = output["financial_resolution"]
    line_total = round(sum(line["amount_brl"] for line in money["refund_lines"]), 2)
    if not same_amount(line_total, money["recommended_refund_brl"]) and not (
        line_total == 0 and money["recommended_refund_brl"] == 0
    ):
        failed.append("REFUND_LINES_TOTAL")
        money["recommended_refund_brl"] = line_total
    status = output["assessment"]["case_status"]
    if status == "no_action" and money["recommended_refund_brl"] > 0:
        failed.append("NO_ACTION_WITH_REFUND")
    if status == "action_required" and not output["resolution_actions"]:
        failed.append("ACTION_REQUIRED_WITHOUT_ACTION")
    if len(set(output["resolution_actions"])) != len(output["resolution_actions"]):
        failed.append("DUPLICATE_ACTIONS")
        output["resolution_actions"] = list(dict.fromkeys(output["resolution_actions"]))
    if not 0 <= output["assessment"]["confidence"] <= 1:
        failed.append("CONFIDENCE_BOUNDS")

    try:
        ctx.trace.contracts.validate_output(output, f"outputs/{ctx.case_id}.json")
    except ContractError:
        failed.append("SCHEMA")
    return output, failed
