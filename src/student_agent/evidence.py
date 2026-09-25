from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1.5
NOT_FOUND_MARKERS = ("not found", "not_found", "no such", "no rows")


@dataclass(frozen=True)
class Evidence:
    ref: str
    tool: str
    domain: str
    data: Any
    warnings: tuple[str, ...] = ()


@dataclass
class ToolFailure:
    tool: str
    code: str
    message: str


@dataclass
class EvidenceLedger:
    """Evidence obtained for exactly one case. Refs never leave this case."""

    case_id: str
    items: dict[str, Evidence] = field(default_factory=dict)
    by_tool: dict[str, Evidence] = field(default_factory=dict)
    failures: list[ToolFailure] = field(default_factory=list)

    def add(self, tool: str, envelope: dict[str, Any]) -> Evidence:
        evidence = Evidence(
            envelope["evidence_ref"],
            tool,
            envelope["domain"],
            envelope.get("data"),
            tuple(envelope.get("warnings") or ()),
        )
        self.items[evidence.ref] = evidence
        self.by_tool[tool] = evidence
        return evidence

    def get(self, tool: str) -> Evidence | None:
        return self.by_tool.get(tool)

    def ref(self, tool: str) -> str | None:
        evidence = self.by_tool.get(tool)
        return evidence.ref if evidence else None

    def owns(self, ref: str) -> bool:
        return ref in self.items


class ToolRunner:
    """Scoped MCP access for one actor: permission check, bounded retry, trace linkage."""

    def __init__(
        self,
        actor: str,
        allowed: tuple[str, ...],
        gateway: EvidenceGateway,
        trace: TraceWriter,
        ledger: EvidenceLedger,
        discovered: frozenset[str],
    ) -> None:
        self.actor = actor
        self.allowed = allowed
        self.gateway = gateway
        self.trace = trace
        self.ledger = ledger
        self.discovered = discovered

    async def fetch(self, tool: str, **arguments: str) -> Evidence | None:
        if tool not in self.allowed:
            raise PermissionError(f"{self.actor} may not call {tool}")
        if tool not in self.discovered:
            self.ledger.failures.append(ToolFailure(tool, "TOOL_NOT_DISCOVERED", tool))
            return None
        cached = self.ledger.get(tool)
        if cached is not None:
            return cached
        last_error: Exception | None = None
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                envelope = await self.gateway.call(
                    tool, case_id=self.ledger.case_id, **arguments
                )
            except RuntimeError as exc:
                # The tool itself answered with an error (e.g. no rows in scope): not retryable.
                code = "NOT_FOUND" if any(m in str(exc).lower() for m in NOT_FOUND_MARKERS) \
                    else "TOOL_ERROR"
                self.ledger.failures.append(ToolFailure(tool, code, str(exc)[:160]))
                return None
            except ValueError as exc:
                self.ledger.failures.append(
                    ToolFailure(tool, "INVALID_EVIDENCE", str(exc)[:160])
                )
                return None
            except Exception as exc:  # timeouts and transport errors from httpx/anyio
                last_error = exc
            else:
                evidence = self.ledger.add(tool, envelope)
                self.trace.emit(
                    case_id=self.ledger.case_id,
                    event_type="tool_result_consumed",
                    actor=self.actor,
                    tool_name=tool,
                    evidence_refs=[evidence.ref],
                    attributes={"domain": evidence.domain, "attempt": attempt},
                )
                return evidence
            if attempt < RETRY_ATTEMPTS:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)
        self.ledger.failures.append(
            ToolFailure(tool, "MCP_UNAVAILABLE", str(last_error)[:160] if last_error else "")
        )
        return None
