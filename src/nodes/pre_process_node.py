"""AgentCore Platform v1.0"""

# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants, never plain strings
#  - Read input_context via state.get("input_context", {}) — read-only
#  - Never import from mediator/, api/, or other agents

from typing import Any, ClassVar, Dict

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.pii import strip_pii_text
from src.validation import (
    MAX_PAYLOAD_BYTES,
    InputRejected,
    inert_identifier,
    screen_text,
)


class PreProcessNode(FunctionNode):
    """Input validation boundary: screen and redact caller input before the workflow runs.

    Three guarantees are established here, before any domain node sees the
    payload:

      * the payload is present, textual, and within the size cap;
      * it carries no prompt-injection payload, screened both raw and with
        markup stripped;
      * worker identifiers are redacted, so no personal data is written to
        State even if the payload later fails to parse.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context: Dict[str, Any] = state.get("input_context") or {}

        if not user_input or not isinstance(user_input, str) or not user_input.strip():
            emit_trace_event("shift_input_rejected", {"reason": "empty_input"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["Input rejected: user_input is empty or missing"],
            }

        if len(user_input.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            emit_trace_event(
                "shift_input_rejected",
                {"reason": "payload_too_large", "limit_bytes": MAX_PAYLOAD_BYTES},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"Input rejected: payload exceeds {MAX_PAYLOAD_BYTES} bytes"],
            }

        try:
            screened = screen_text(user_input, field="user_input")
        except InputRejected as exc:
            # Refusal happens here, in the node that owns the caller contract —
            # not only in a framework gate that may be configured off. The
            # field is named; the rejected text is never echoed.
            emit_trace_event(
                "shift_input_rejected",
                {"reason": "injection_screen", "field": exc.field},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"Input rejected: {exc}"],
            }

        # Redact worker identifiers before the payload is stored. State must
        # never hold the raw form, so this runs ahead of the domain workflow.
        redacted = strip_pii_text(screened.strip())

        # The channel hint is caller-supplied and is echoed in enriched_context,
        # so it is locked to the inert identifier alphabet.
        try:
            channel = inert_identifier(input_context.get("channel"), field="input_context.channel", default="unknown")
        except InputRejected as exc:
            emit_trace_event(
                "shift_input_rejected",
                {"reason": "invalid_channel", "field": exc.field},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"Input rejected: {exc}"],
            }

        emit_trace_event(
            "shift_input_accepted",
            {"input_chars": len(redacted), "channel": channel},
            state,
        )

        return {
            "validated_input": redacted,
            "enriched_context": {
                "source": "manufacturing_shift_report_agent",
                "channel": channel,
            },
            "status": AgentStatus.SUCCESS.value,
        }
