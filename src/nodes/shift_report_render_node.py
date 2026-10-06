"""AgentCore Platform v1.0"""

# MFG-C2-004 — ShiftReportRenderNode
# Domain node 5: render the final structured shift-end report.
#
# Combines the computed production metrics, the narrative summary, and the
# detected anomalies into a Markdown report.
#
# Rendering strategy: the report is built by _render_builtin(), a plain Python
# function that always produces a complete document. An external template file
# is used only when a template engine is installed AND the rendered result
# contains no residual template syntax. That guard exists because the engine is
# an optional dependency: without it, substituting into a template file leaves
# loops and conditionals unexpanded, and the caller receives markup instead of
# a report.
#
# Access scoping — which shifts a given caller may query — is enforced by the
# caller-facing gateway, not inside agent state. The agent enforces the trust
# level required to invoke it at all.

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.validation import domain_config

logger = logging.getLogger(__name__)

_DEFAULT_TEMPLATE_PATH = "config/shift_report_template.md"

# Any residual placeholder or block tag means the render did not complete.
_RESIDUAL_TEMPLATE_RE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)

# Metric rows rendered in the production summary, in display order.
_METRIC_ROWS = (
    ("OEE", "oee", True),
    ("Availability", "availability", True),
    ("Performance Rate", "performance_rate", True),
    ("Quality Rate", "quality_rate", True),
    ("Units Produced", "units_produced", False),
    ("Units Target", "units_target", False),
    ("Units Variance", "units_variance", False),
    ("Downtime (min)", "downtime_minutes", False),
    ("Reject Rate", "reject_rate", True),
)


def _format_metric(value: Any, as_percent: bool = False) -> str:
    """Format a metric value for display."""
    if value is None:
        return "N/A"
    if isinstance(value, bool):
        return "N/A"
    if as_percent and isinstance(value, (int, float)):
        return f"{float(value) * 100:.1f}%"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _extract_action_items(summary_text: str) -> List[str]:
    """Extract action items from the narrative's Action Items section."""
    if not summary_text:
        return []

    items: List[str] = []
    in_action_section = False

    for line in summary_text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(("## action", "# action", "action items")):
            in_action_section = True
            continue
        if in_action_section and stripped.startswith("#"):
            break
        if (
            in_action_section
            and stripped
            and (
                stripped.startswith(("-", "*", "•"))
                or (len(stripped) > 2 and stripped[0].isdigit() and stripped[1] in ".)")
            )
        ):
            item = stripped.lstrip("-*•0123456789.) ").strip()
            if item:
                items.append(item)

    return items


def _render_builtin(context: Dict[str, Any]) -> str:
    """Build the complete Markdown report in plain Python.

    This is the guaranteed path: it depends on nothing beyond the standard
    library and can never emit unexpanded markup.
    """
    metrics: Dict[str, str] = context["metrics"]
    anomalies: List[Dict[str, Any]] = context["anomaly_highlights"]
    action_items: List[str] = context["action_items"]

    lines: List[str] = [
        "# Manufacturing Shift Report",
        "",
        f"**Shift ID:** {context['shift_id']}",
        f"**Generated:** {context['generated_at']}",
        "",
        "---",
        "",
        "## Production Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
    ]
    for label, key, _ in _METRIC_ROWS:
        lines.append(f"| {label} | {metrics.get(key, 'N/A')} |")

    lines += ["", "---", "", "## Narrative Summary", "", context["summary_text"], "", "---", ""]

    lines.append("## Anomaly Highlights")
    lines.append("")
    if anomalies:
        for anomaly in anomalies:
            lines.append(
                f"- **[{anomaly.get('severity', 'UNKNOWN')}] "
                f"{anomaly.get('anomaly_type', 'anomaly')}** — "
                f"{anomaly.get('description', '')}"
            )
    else:
        lines.append("No anomalies detected during this shift.")

    lines += ["", "---", "", "## Action Items", ""]
    if action_items:
        for index, item in enumerate(action_items, 1):
            lines.append(f"{index}. {item}")
    else:
        lines.append("No specific action items generated for this shift.")

    lines += [
        "",
        "---",
        "",
        "*Manufacturing shift report — generated from the submitted shift log.*",
    ]
    return "\n".join(lines)


def _render_with_template_engine(template_str: str, context: Dict[str, Any]) -> Optional[str]:
    """Render via the template engine when it is installed.

    Returns None whenever the engine is unavailable or the render is
    incomplete, so the caller falls back to the built-in renderer.
    """
    try:
        from jinja2 import Environment, StrictUndefined
    except ImportError:
        logger.info(
            "ShiftReportRenderNode: template engine not installed; " "rendering the report with the built-in renderer"
        )
        return None

    try:
        env = Environment(undefined=StrictUndefined, autoescape=False)
        rendered: str = str(env.from_string(template_str).render(**context))
    except Exception as exc:  # noqa: BLE001 — any template fault falls back
        logger.warning(
            "ShiftReportRenderNode: template render failed (%s); " "rendering the report with the built-in renderer",
            type(exc).__name__,
        )
        return None

    if _RESIDUAL_TEMPLATE_RE.search(rendered):
        logger.warning(
            "ShiftReportRenderNode: rendered report still contains template syntax; "
            "rendering the report with the built-in renderer"
        )
        return None

    return rendered


def _load_template(template_path: str) -> Optional[str]:
    """Load the report template from disk; None when it cannot be read."""
    if not template_path:
        return None

    for path in (
        template_path,
        os.path.join(os.path.dirname(__file__), "..", "..", template_path),
    ):
        abs_path = os.path.abspath(path)
        if os.path.isfile(abs_path):
            try:
                with open(abs_path, encoding="utf-8") as handle:
                    return handle.read()
            except OSError:
                logger.warning("ShiftReportRenderNode: report template could not be read")

    logger.info("ShiftReportRenderNode: no report template file found; using the built-in layout")
    return None


class ShiftReportRenderNode(FunctionNode):
    """Render the final structured shift-end report.

    Input state keys:
        shift_id:           validated shift identifier (from LogIngestionNode)
        summary_text:       narrative (from SummarizationNode)
        anomaly_highlights: detected anomalies (from AnomalyDetectionNode)
        metrics:            production metrics (from DataNormalizationNode)

    Output state keys (partial dict):
        shift_report: rendered Markdown report
        status:       AgentStatus value for the inner graph run
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.INTERNAL

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """Capture the runtime config handed down by the inner graph."""
        self._cfg = domain_config(config)

    def execute(self, state: AgentState) -> Dict[str, Any]:
        summary_text: str = state.get("summary_text") or ""
        anomaly_highlights: List[Dict[str, Any]] = state.get("anomaly_highlights") or []
        metrics: Dict[str, Any] = state.get("metrics") or {}

        template_path = str(self._cfg.get("report_template_path") or _DEFAULT_TEMPLATE_PATH)

        # The shift identifier is read from the key LogIngestionNode writes in
        # THIS graph's state. Re-parsing it out of the caller payload here
        # would read a key the inner graph never receives, and the header would
        # silently render UNKNOWN on every real invocation.
        shift_id = str(state.get("shift_id") or "UNKNOWN")

        formatted_metrics = {
            key: _format_metric(metrics.get(key), as_percent=as_percent) for _, key, as_percent in _METRIC_ROWS
        }

        action_items = _extract_action_items(summary_text)

        render_context: Dict[str, Any] = {
            "shift_id": shift_id,
            "generated_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "metrics": formatted_metrics,
            "summary_text": summary_text or "(No summary generated)",
            "anomaly_highlights": anomaly_highlights,
            "action_items": action_items,
        }

        template_str = _load_template(template_path)
        shift_report = None
        renderer = "builtin"
        if template_str is not None:
            shift_report = _render_with_template_engine(template_str, render_context)
            if shift_report is not None:
                renderer = "template"
        if shift_report is None:
            shift_report = _render_builtin(render_context)

        emit_trace_event(
            "shift_report_rendered",
            {
                "renderer": renderer,
                "report_chars": len(shift_report),
                "anomaly_count": len(anomaly_highlights),
                "action_item_count": len(action_items),
                "shift_id": shift_id,
            },
            state,
        )

        logger.info(
            "ShiftReportRenderNode: report rendered via %s (%d chars, %d anomalies)",
            renderer,
            len(shift_report),
            len(anomaly_highlights),
        )

        return {
            "shift_report": shift_report,
            "status": AgentStatus.SUCCESS.value,
        }
