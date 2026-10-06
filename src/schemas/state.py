"""AgentCore Platform v1.0"""

# State must be a flat TypedDict — never a Pydantic model. LangGraph
# checkpoints use msgpack serialization, which corrupts Pydantic objects
# silently. Extend AgentState with agent-specific fields only, and never add
# credentials or secrets.
#
# MFG-C2-004 — Manufacturing Shift Report & Incident Summarization Agent
# Two-layer nested Cat 2 graph: outer backbone (AgentBaseGraph) + inner
# domain workflow (BaseGraph). The fields below cover both layers.
#
# PII note: worker identifiers are redacted by PreProcessNode before the
# payload is written to State, and again on each record retained by
# LogIngestionNode. Domain nodes never see a raw worker identifier.

from typing import Any, Dict, List, Optional

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Flat TypedDict for MFG-C2-004.

    All shared fields (user_input, status, session_id, node_history,
    error_log, hitl_*, etc.) are inherited from AgentState.
    """

    # ------------------------------------------------------------------
    # Outer layer — set by PreProcessNode / ShiftReportGraphNode.merge_output
    # ------------------------------------------------------------------

    # Screened, PII-redacted shift-log payload produced by PreProcessNode.
    # The raw caller input is not persisted beyond that node.
    validated_input: Optional[str]

    # Final rendered shift-end report (Markdown / structured text).
    # Written by ShiftReportRenderNode; surfaced via merge_output.
    shift_report: Optional[str]

    # ------------------------------------------------------------------
    # Inner layer — domain nodes (DomainWorkflowGraph)
    # ------------------------------------------------------------------

    # Validated shift identifier, written by LogIngestionNode from the caller
    # payload and read by ShiftReportRenderNode for the report header. It is
    # written in the INNER graph, which is the graph that reads it — a reader
    # in one layer for a key written only in the other would compare against an
    # absent value on every invocation.
    shift_id: Optional[str]

    # LogIngestionNode outputs
    # Parsed MES log entries; worker identifiers already redacted.
    # Each record: {"timestamp": str, "event_type": str,
    #               "machine_id": str, "value": Any, ...}
    raw_log_records: Optional[List[Dict[str, Any]]]

    # Reasons for malformed / rejected log entries. Field names only — a
    # rejected value is never echoed here.
    log_ingestion_errors: Optional[List[str]]

    # DataNormalizationNode outputs
    # Records normalized to canonical schema; timestamps ISO-8601,
    # units SI, alarm codes mapped to descriptions.
    normalized_records: Optional[List[Dict[str, Any]]]

    # AnomalyDetectionNode outputs
    # Detected stoppages / out-of-threshold events / quality outliers.
    # Each entry: {"event_type": str, "machine_id": str,
    #              "duration_minutes": float, "cause": str, ...}
    anomaly_highlights: Optional[List[Dict[str, Any]]]

    # Computed production metrics for the shift.
    # Keys match config.metrics_to_compute:
    #   oee, availability, performance_rate, quality_rate,
    #   units_produced, units_target, downtime_minutes, reject_rate
    metrics: Optional[Dict[str, Any]]

    # SummarizationNode output
    # Narrative summary (summary + action items). Raw log records are never
    # carried into it — only aggregate counts, metrics and anomaly text.
    summary_text: Optional[str]

    # ------------------------------------------------------------------
    # Tracing / audit — framework-managed; do NOT write from node code
    # ------------------------------------------------------------------

    trace_id: Optional[str]
    correlation_id: Optional[str]
    # node_history inherited from AgentState; listed here for clarity
    # node_history: Optional[List[str]]
