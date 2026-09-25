"""Deterministic L3A decision rules. No LLM: every conclusion is derived from MCP facts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .facts import CaseFacts, same_amount

FALLBACK_RULES: dict[str, dict[str, Any]] = {
    "insufficient_evidence": {
        "case_status": "needs_investigation",
        "recommended_action": "escalate_manual_review",
        "refund_brl": 0.0,
        "responsible_parties": [{"party_type": "unknown", "party_id": None}],
    },
}

# Evidence groups each conclusion depends on (tool names, cited in this order).
CITATIONS: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": ("get_order", "get_payment_timeline", "get_policy"),
    "unavailable_order_paid": (
        "get_order", "get_order_items", "get_sellers", "get_payment_timeline", "get_policy"
    ),
    "late_delivery_seller": (
        "get_order", "get_order_items", "get_sellers", "get_shipment_summary", "get_policy"
    ),
    "late_delivery_logistics": (
        "get_order", "get_order_items", "get_shipment_summary", "get_policy"
    ),
    "valid_split_payment": ("get_order", "get_order_items", "get_payment_timeline", "get_policy"),
    "payment_mismatch": ("get_order", "get_payment_timeline", "get_policy"),
    "duplicate_charge": ("get_order", "get_order_items", "get_payment_timeline", "get_policy"),
    "refund_pending": ("get_order", "get_payment_timeline", "get_refund_timeline", "get_policy"),
    "refund_failed": ("get_order", "get_payment_timeline", "get_refund_timeline", "get_policy"),
    "unsupported_claim": (
        "get_order", "get_shipment_summary", "get_payment_timeline", "get_policy"
    ),
    "insufficient_evidence": ("get_order",),
}

SELLER_ISSUES = {"unavailable_order_paid", "late_delivery_seller"}


@dataclass
class Decision:
    primary_issue: str
    case_status: str
    confidence: float
    recommended_action: str
    refund_brl: float
    evidence_amount: float | None
    responsible_parties: list[dict[str, Any]]
    cause_codes: list[str]
    citations: tuple[str, ...]
    signals: list[str] = field(default_factory=list)


def classify(facts: CaseFacts) -> tuple[str, list[str], float | None]:
    """Return (primary_issue, observed signal codes, evidence-derived amount)."""
    if not facts.order_found:
        return "insufficient_evidence", ["ORDER_NOT_FOUND"], None
    captured = facts.captured_total
    status = facts.order_status or ""
    if status == "canceled" and captured > 0:
        return "canceled_order_paid", ["ORDER_CANCELED", "PAYMENT_CAPTURED"], captured
    if status == "unavailable" and captured > 0:
        return "unavailable_order_paid", ["ORDER_UNAVAILABLE", "PAYMENT_CAPTURED"], captured

    failed = [e for e in facts.refunds if e.status == "failed"]
    if failed:
        return "refund_failed", ["REFUND_FAILED"], failed[-1].amount
    pending = [e for e in facts.refunds if e.status in {"pending", "processing", "requested"}]
    if pending:
        return "refund_pending", ["REFUND_PENDING"], 0.0

    mismatch = [e for e in facts.payment_flags if "mismatch" in e.kind]
    if mismatch:
        return "payment_mismatch", ["RECONCILIATION_MISMATCH"], mismatch[-1].amount

    amounts = [e.amount for e in facts.captures if e.amount is not None]
    total = facts.order_total
    if len(amounts) >= 2:
        repeated = [a for a in set(amounts) if amounts.count(a) >= 2]
        if repeated and not same_amount(sum(amounts), total):
            return "duplicate_charge", ["REPEATED_CAPTURE", "CAPTURED_EXCEEDS_ORDER"], max(repeated)

    if facts.is_late:
        handoff_late = facts.seller_handoff_late
        if handoff_late is True:
            return "late_delivery_seller", ["DELIVERED_LATE", "SELLER_HANDOFF_LATE"], captured
        if handoff_late is False:
            return (
                "late_delivery_logistics",
                ["DELIVERED_LATE", "SELLER_HANDOFF_ON_TIME"],
                captured,
            )
        return "insufficient_evidence", ["DELIVERED_LATE", "HANDOFF_UNKNOWN"], None

    if len(amounts) >= 2 and same_amount(sum(amounts), total):
        return "valid_split_payment", ["SPLIT_PAYMENT_MATCHES_ORDER"], 0.0
    return "unsupported_claim", ["NO_ANOMALY_IN_EVIDENCE"], 0.0


def _parties(issue: str, rule: dict[str, Any], facts: CaseFacts) -> list[dict[str, Any]]:
    parties: list[dict[str, Any]] = []
    for party in rule.get("responsible_parties") or []:
        party_type = party.get("party_type", "unknown")
        if party_type == "seller":
            # The policy lists an illustrative seller; attribute to this order's seller(s).
            parties += [{"party_type": "seller", "party_id": s} for s in facts.seller_ids]
            if not facts.seller_ids:
                parties.append({"party_type": "seller", "party_id": None})
        else:
            parties.append({"party_type": party_type, "party_id": None})
    if not parties:
        default = "seller" if issue in SELLER_ISSUES else "unknown"
        parties.append({"party_type": default, "party_id": None})
    return parties[:5]


def decide(facts: CaseFacts, claimed_topics: list[str]) -> Decision:
    issue, signals, evidence_amount = classify(facts)
    cause_codes = list(dict.fromkeys([issue.upper(), *signals]))[:5]
    rule = facts.policy_rules.get(issue) or FALLBACK_RULES.get(issue)
    if rule is None:
        signals.append("POLICY_RULE_MISSING")
        rule = {
            "case_status": "needs_investigation",
            "recommended_action": "escalate_manual_review",
            "refund_brl": 0.0,
            "responsible_parties": [],
        }
    policy_refund = rule.get("refund_brl")
    refund = float(policy_refund) if isinstance(policy_refund, int | float) else None
    if refund is None:
        refund = evidence_amount or 0.0
        signals.append("REFUND_FROM_EVIDENCE")

    confidence = 0.95
    if issue == "insufficient_evidence":
        confidence = 0.6
    if evidence_amount is not None and refund > 0 and not same_amount(refund, evidence_amount):
        signals.append("POLICY_AMOUNT_DIFFERS_FROM_EVIDENCE")
        confidence -= 0.1
    if claimed_topics and issue not in claimed_topics:
        signals.append("CLAIM_NOT_CORROBORATED")
        confidence -= 0.15

    return Decision(
        primary_issue=issue,
        case_status=rule.get("case_status", "needs_investigation"),
        confidence=round(max(0.05, min(confidence, 0.99)), 2),
        recommended_action=rule.get("recommended_action", "escalate_manual_review"),
        refund_brl=round(refund, 2),
        evidence_amount=evidence_amount,
        responsible_parties=_parties(issue, rule, facts),
        cause_codes=cause_codes,
        citations=CITATIONS.get(issue, ("get_order",)),
        signals=signals,
    )
