"""AgentCore Platform v1.0"""

# MFG-C2-004 — PostProcessNode
# Outer post_process slot: the output boundary for the rendered shift report.
#
# Two independent checks run here, each with its own audit event:
#
#   1. Credential scan, using the framework's own detector. Using the same
#      function the framework's gate calls keeps the two block sets identical.
#      A narrower local pattern set would be a bypass, not a simplification:
#      a value this node passed and the framework caught would make the
#      framework raise *after* this node returned, and the wrapper discards the
#      node's whole delta on an exception — including the clearing below.
#   2. Worker-identifier scan, covering the case where a redaction upstream did
#      not attribute an identifier carried in an unexpected position.
#
# On a violation the node returns ERROR *and clears every output-bearing
# field*. Clearing is the containment step, not the error status:
# AgentBaseGraph.get_output resolves output as
# ``state.get("formatted_output") or state.get("result")`` with no status
# check, so returning ERROR while leaving `result` populated would still ship
# the ungated report inside the error envelope. A falsy formatted_output would
# do the same by re-activating the fallback, so the withheld notice is truthy.

from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from framework.security.credential_detector import detect_credentials_in_value
from shared.utils.audit_logger import emit_trace_event

from src.pii import contains_pii

# Returned in place of the report when the output boundary refuses it. Truthy
# by construction: an empty string here would fall through to `result`.
WITHHELD_NOTICE = (
    "Shift report withheld: the output boundary refused to release it. "
    "No report content is included in this response."
)

# Output-bearing fields cleared on refusal. Every field that can carry report
# text or a caller payload must appear here; provenance fields (status,
# counters) are not output-bearing and are deliberately absent.
_OUTPUT_BEARING_FIELDS = ("result", "shift_report", "summary_text")


class PostProcessNode(FunctionNode):
    """Format and finalize the shift report, enforcing the output boundary.

    Reads the rendered report from ``state["result"]``, which is populated by
    ShiftReportGraphNode.merge_output() from the inner graph's ``shift_report``
    output. Both halves of that mapping are asserted by the boundary tests —
    a layer that reads a key nothing writes at this level would compare against
    an empty value on every invocation and pass silently.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        result = state.get("result") or ""

        if not isinstance(result, str):
            result = str(result)

        # An upstream failure reaches the boundary with no report to release.
        if not result.strip():
            emit_trace_event(
                "shift_report_empty",
                {"reason": "no_report_produced"},
                state,
            )
            return {
                "formatted_output": (
                    "No shift report was produced. Check the ingestion errors " "reported for this shift."
                ),
                "status": AgentStatus.SUCCESS.value,
            }

        violations: List[str] = []

        # 1. Credential scan — the framework's own detector, so this node's
        #    refusal set matches the framework block set exactly.
        if detect_credentials_in_value(result):
            violations.append("credential_pattern")

        # 2. Worker-identifier scan.
        if contains_pii(result):
            violations.append("worker_identifier")

        if violations:
            return self._withhold(state, violations)

        emit_trace_event(
            "shift_report_released",
            {"report_chars": len(result), "checks_passed": 2},
            state,
        )

        return {
            "formatted_output": result,
            "status": AgentStatus.SUCCESS.value,
        }

    def _withhold(self, state: AgentState, violations: List[str]) -> Dict[str, Any]:
        """Refuse the report and clear every field that could still carry it.

        The audit event and the returned message name the CHECK that fired and
        nothing else — never the matched text, which would put the very value
        the gate refused into the audit trail and the error envelope.
        """
        emit_trace_event(
            "shift_report_withheld",
            {"checks_failed": sorted(violations)},
            state,
        )

        cleared: Dict[str, Any] = {field: "" for field in _OUTPUT_BEARING_FIELDS}
        cleared.update(
            {
                "formatted_output": WITHHELD_NOTICE,
                "status": AgentStatus.ERROR.value,
                "error_log": ["Output boundary refused the shift report " f"({', '.join(sorted(violations))})."],
            }
        )
        return cleared
