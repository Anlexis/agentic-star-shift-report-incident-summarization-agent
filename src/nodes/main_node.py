"""AgentCore Platform v1.0"""

from typing import Any, ClassVar, Dict

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class MainNode(FunctionNode):
    """Single-step core-logic node — not wired into this agent's graph.

    The `main` slot of the outer graph is filled by ShiftReportGraphNode, which
    delegates to the inner domain workflow. This node is retained as the
    reference shape for a single-step `main` implementation and performs a
    passthrough of the validated input.

    It is instrumented like any other boundary node: if it is ever wired into a
    graph, its execution is auditable from the first invocation rather than
    from whenever someone remembers to add the event.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        validated_input = state.get("validated_input", state.get("user_input", ""))

        emit_trace_event(
            "main_passthrough",
            {"input_chars": len(validated_input or "")},
            state,
        )

        return {
            "result": validated_input,
            "status": AgentStatus.SUCCESS.value,
        }
