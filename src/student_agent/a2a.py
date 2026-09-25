from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any

from .trace import TraceWriter

MAX_HOPS = 16


@dataclass(frozen=True)
class A2AMessage:
    """Envelope exchanged between agents. Correlated by case_id; hop bounds loops."""

    case_id: str
    sender: str
    recipient: str
    intent: str
    payload: dict[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    hop: int = 0
    message_id: str = field(default_factory=lambda: f"msg_{secrets.token_hex(8)}")


class A2ABus:
    """In-process agent bus. Every message becomes an observable trace event."""

    def __init__(self, case_id: str, trace: TraceWriter) -> None:
        self.case_id = case_id
        self.trace = trace
        self.hops = 0

    def _next_hop(self) -> int:
        self.hops += 1
        if self.hops > MAX_HOPS:
            raise RuntimeError(f"{self.case_id}: A2A hop limit exceeded")
        return self.hops

    def assign(self, sender: str, recipient: str, task: str, **payload: Any) -> A2AMessage:
        message = A2AMessage(
            self.case_id, sender, recipient, task, dict(payload), hop=self._next_hop()
        )
        self.trace.emit(
            case_id=self.case_id,
            event_type="task_assigned",
            actor=sender,
            target=recipient,
            decision_code=task,
            attributes={"message_id": message.message_id, "hop": message.hop},
        )
        return message

    def handoff(
        self,
        sender: str,
        recipient: str,
        intent: str,
        evidence_refs: list[str] | tuple[str, ...] = (),
        **payload: Any,
    ) -> A2AMessage:
        refs = tuple(dict.fromkeys(evidence_refs))
        message = A2AMessage(
            self.case_id, sender, recipient, intent, dict(payload), refs, self._next_hop()
        )
        self.trace.emit(
            case_id=self.case_id,
            event_type="handoff",
            actor=sender,
            target=recipient,
            decision_code=intent,
            evidence_refs=list(refs[:20]) or None,
            attributes={"message_id": message.message_id, "hop": message.hop},
        )
        return message
