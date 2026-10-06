"""AgentCore Platform v1.0"""

# MFG-C2-004 — SummarizationNode
# Domain node 4: narrative summarization of the shift.
#
# The LLM service is injected at construction, following the framework's own
# LLMCallNode contract (``complete(messages) -> {"content": str, ...}``).
# Injection is the only channel available: the framework calls
# ``execute(state)`` with no config argument, and InvocationContext carries
# identity and secrets, not service handles.
#
# When no LLM service is provisioned the node composes the narrative from the
# metrics and anomalies already computed by the pipeline. That path is a real
# summary of real data, not a placeholder — the report a caller receives is
# always derived from their own shift log.
#
# Raw log records are never forwarded into the narrative. Only aggregate
# counts, computed metrics and anomaly descriptions cross into the summary.

import logging
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.validation import InputRejected, domain_config, finite_in_range

logger = logging.getLogger(__name__)

_DEFAULT_SYSTEM_PROMPT = (
    "You are a manufacturing shift report assistant. "
    "Summarize the following shift log data, highlighting anomalies, stoppages, "
    "and action items. Use structured output with sections: Summary, Anomalies, "
    "Production Metrics, Action Items."
)

# Ceiling on the narrative accepted back from an LLM service, independent of
# the configured max_tokens, so a misbehaving service cannot flood the report.
_NARRATIVE_CHAR_CEILING = 20_000


def _build_metrics_summary(metrics: Dict[str, Any]) -> str:
    """Format the computed metrics as a compact text block."""
    if not metrics:
        return "No production metrics available."
    labels = (
        ("shift_duration_minutes", "Shift duration", "{:.0f} min"),
        ("oee", "OEE", "{:.1%}"),
        ("availability", "Availability", "{:.1%}"),
        ("performance_rate", "Performance", "{:.1%}"),
        ("quality_rate", "Quality rate", "{:.1%}"),
        ("units_produced", "Units produced", "{:.0f}"),
        ("units_target", "Units target", "{:.0f}"),
        ("units_variance", "Units variance", "{:+.0f}"),
        ("downtime_minutes", "Downtime", "{:.1f} min"),
        ("reject_rate", "Reject rate", "{:.2%}"),
    )
    lines: List[str] = []
    for key, label, fmt in labels:
        if key in metrics:
            try:
                lines.append(f"  {label}: {fmt.format(float(metrics[key]))}")
            except (TypeError, ValueError):
                continue
    return "\n".join(lines) if lines else "No metrics."


def _build_anomaly_summary(anomalies: List[Dict[str, Any]]) -> str:
    """Format anomaly highlights as a compact list."""
    if not anomalies:
        return "No anomalies detected."
    return "\n".join(
        f"  {i}. [{a.get('severity', '')}] {a.get('anomaly_type', '')}: " f"{a.get('description', '')}"
        for i, a in enumerate(anomalies, 1)
    )


def _build_record_digest(records: List[Dict[str, Any]]) -> str:
    """Summarise the shift log by event-type counts only.

    Counts are the whole digest by design. Forwarding sample records would
    place raw log content into the narrative and, through it, into the
    published report.
    """
    if not records:
        return "No shift log records."
    event_counts: Dict[str, int] = {}
    for rec in records:
        event_type = str(rec.get("event_type", "UNKNOWN")).upper()
        event_counts[event_type] = event_counts.get(event_type, 0) + 1
    breakdown = ", ".join(f"{k}: {v}" for k, v in sorted(event_counts.items()))
    return f"Total records: {len(records)} ({breakdown})"


def _compose_narrative(
    metrics: Dict[str, Any],
    anomalies: List[Dict[str, Any]],
    records: List[Dict[str, Any]],
) -> str:
    """Compose the narrative from computed values, with no LLM service.

    Produces the same four sections an LLM is asked for, so the report
    structure does not depend on whether a service is provisioned.
    """
    oee = metrics.get("oee")
    downtime = metrics.get("downtime_minutes", 0.0)
    produced = metrics.get("units_produced", 0.0)
    target = metrics.get("units_target", 0.0)

    if oee is None:
        headline = "No production metrics could be computed for this shift."
    else:
        attainment = f"{produced:.0f} of {target:.0f} units" if target else f"{produced:.0f} units"
        headline = (
            f"Shift completed with an OEE of {float(oee):.1%}, producing {attainment} "
            f"against {float(downtime):.1f} minutes of recorded downtime."
        )

    high = [a for a in anomalies if str(a.get("severity", "")).upper() == "HIGH"]
    if not anomalies:
        anomaly_text = "No anomalies were detected during this shift."
    else:
        anomaly_text = (
            f"{len(anomalies)} anomaly record(s) were detected, "
            f"{len(high)} of them at HIGH severity.\n"
            + "\n".join(
                f"- [{a.get('severity', '')}] {a.get('anomaly_type', '')}: " f"{a.get('description', '')}"
                for a in anomalies
            )
        )

    actions: List[str] = []
    for anomaly in high:
        machine = anomaly.get("machine_id", "the affected asset")
        kind = str(anomaly.get("anomaly_type", "issue")).replace("_", " ")
        actions.append(f"Investigate {kind} on {machine} before the next shift.")
    if isinstance(oee, (int, float)) and float(oee) < 0.65:
        actions.append("Review the shift for OEE recovery — attainment is below target.")
    if not actions:
        actions.append("No follow-up required; continue routine monitoring.")

    return (
        "## Summary\n"
        f"{headline}\n\n"
        "## Anomalies\n"
        f"{anomaly_text}\n\n"
        "## Production Metrics\n"
        f"{_build_metrics_summary(metrics)}\n\n"
        "## Action Items\n" + "\n".join(f"- {item}" for item in actions)
    )


class SummarizationNode(FunctionNode):
    """Produce the narrative section of the shift report.

    Input state keys:
        normalized_records:  canonical records from DataNormalizationNode
        anomaly_highlights:  detected anomalies from AnomalyDetectionNode
        metrics:             OEE and production metrics

    Output state keys (partial dict):
        summary_text: the narrative (no raw log records are carried into it)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.INTERNAL

    def __init__(self, config: Optional[Dict[str, Any]] = None, llm: Optional[Any] = None) -> None:
        """Capture the runtime config and the optional LLM service.

        ``llm`` implements the framework BaseLLM contract; any object exposing
        ``complete(messages) -> dict`` satisfies it.
        """
        self._cfg = domain_config(config)
        self._llm_cfg: Dict[str, Any] = (config or {}).get("llm") or {}
        self._llm = llm

    def execute(self, state: AgentState) -> Dict[str, Any]:
        normalized_records: List[Dict[str, Any]] = state.get("normalized_records") or []
        anomaly_highlights: List[Dict[str, Any]] = state.get("anomaly_highlights") or []
        metrics: Dict[str, Any] = state.get("metrics") or {}

        system_prompt = str(self._cfg.get("system_prompt") or _DEFAULT_SYSTEM_PROMPT).strip()

        user_message = (
            "=== SHIFT LOG DIGEST ===\n"
            f"{_build_record_digest(normalized_records)}\n\n"
            "=== PRODUCTION METRICS ===\n"
            f"{_build_metrics_summary(metrics)}\n\n"
            "=== ANOMALIES ===\n"
            f"{_build_anomaly_summary(anomaly_highlights)}\n\n"
            "Please generate a structured shift report summary with the sections: "
            "Summary, Anomalies, Production Metrics, Action Items."
        )

        summary_text, source = self._summarize(
            system_prompt,
            user_message,
            metrics=metrics,
            anomalies=anomaly_highlights,
            records=normalized_records,
        )

        # Cap the narrative regardless of its source.
        max_chars = self._narrative_cap()
        if len(summary_text) > max_chars:
            summary_text = summary_text[:max_chars]

        emit_trace_event(
            "shift_narrative_generated",
            {
                "source": source,
                "model": str(self._llm_cfg.get("model", "unconfigured")),
                "records_count": len(normalized_records),
                "anomaly_count": len(anomaly_highlights),
                "summary_len": len(summary_text),
            },
            state,
        )

        logger.info(
            "SummarizationNode: narrative generated via %s (%d chars)",
            source,
            len(summary_text),
        )

        return {"summary_text": summary_text}

    def _narrative_cap(self) -> int:
        """Resolve the narrative length cap from the configured token budget."""
        try:
            max_tokens = finite_in_range(
                self._llm_cfg.get("max_tokens"),
                field="llm.max_tokens",
                minimum=1.0,
                maximum=200_000.0,
                default=2048.0,
            )
        except InputRejected:
            logger.warning("SummarizationNode: configured llm.max_tokens is invalid; using the built-in cap")
            return _NARRATIVE_CHAR_CEILING
        # Roughly four characters per token, bounded by the module ceiling.
        return min(int(max_tokens) * 4, _NARRATIVE_CHAR_CEILING)

    def _summarize(
        self,
        system_prompt: str,
        user_message: str,
        *,
        metrics: Dict[str, Any],
        anomalies: List[Dict[str, Any]],
        records: List[Dict[str, Any]],
    ) -> tuple[str, str]:
        """Return the narrative and the source that produced it.

        Falls back to the computed narrative both when no service is
        provisioned and when a provisioned service returns nothing usable —
        an empty narrative would otherwise reach the report as a blank section.
        """
        if self._llm is None:
            return _compose_narrative(metrics, anomalies, records), "computed"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        response = self._llm.complete(messages)
        content = ""
        if isinstance(response, dict):
            content = str(response.get("content") or "")
        elif isinstance(response, str):
            content = response
        if not content.strip():
            logger.warning(
                "SummarizationNode: LLM service returned no content; " "falling back to the computed narrative"
            )
            return _compose_narrative(metrics, anomalies, records), "computed"
        return content, "llm"
