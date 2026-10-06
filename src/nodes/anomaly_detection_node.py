"""AgentCore Platform v1.0"""

# MFG-C2-004 — AnomalyDetectionNode
# Domain node 3: detect anomalous events in normalized shift-log records.
#
# Flags events that exceed the anomaly_highlight_threshold (default: 0.15,
# meaning events whose combined duration exceeds 15% of shift time).
# Also detects alarm bursts (multiple alarms from the same machine in a
# short window) and quality outliers (reject_rate above threshold).
#
# No raw employee data is included in anomaly records (PII rule).
#
# Wired by the inner graph (DomainWorkflowGraph).
# Returns only changed state keys (partial dict).

import logging
from collections import defaultdict
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.validation import InputRejected, domain_config, finite_in_range

logger = logging.getLogger(__name__)

# Default anomaly threshold: events occupying > 15% of shift time are flagged.
_DEFAULT_THRESHOLD = 0.15

# Default shift duration assumed if not in config.
_DEFAULT_SHIFT_MINUTES = 480.0  # 8 hours

# Alarm burst: >= N alarms from the same machine within this window (minutes).
_BURST_WINDOW_MINUTES = 30.0
_BURST_MIN_COUNT = 3

# Quality outlier: reject_rate above this proportion flags an anomaly.
_QUALITY_ANOMALY_THRESHOLD = 0.05  # 5% rejection rate

# Event types that count as downtime/stoppage for threshold comparison.
_DOWNTIME_EVENT_TYPES = {"STOP", "DOWNTIME", "ALARM", "STOPPAGE"}


def _extract_machine_alarm_times(
    records: List[Dict[str, Any]],
) -> Dict[str, List[float]]:
    """Build a map of machine_id -> sorted list of alarm timestamps (epoch-like float).

    Timestamps are treated as opaque sort keys here; we only care about ordering.
    """
    machine_times: Dict[str, List[float]] = defaultdict(list)
    for idx, rec in enumerate(records):
        event_type = str(rec.get("event_type", "")).upper()
        if event_type not in ("ALARM", "FAULT", "ERROR"):
            continue
        machine_id = str(rec.get("machine_id", ""))
        if not machine_id:
            continue
        # Use record index as a proxy order key if no parseable timestamp.
        ts_raw = rec.get("timestamp", "")
        try:
            # Try to interpret ISO timestamp as float (epoch ms fallback).
            from datetime import datetime

            if isinstance(ts_raw, (int, float)):
                ts_float = float(ts_raw)
            else:
                ts_obj = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                ts_float = ts_obj.timestamp()
        except (ValueError, AttributeError):
            ts_float = float(idx)
        machine_times[machine_id].append(ts_float)

    for machine_id in machine_times:
        machine_times[machine_id].sort()
    return dict(machine_times)


def _detect_alarm_bursts(
    machine_times: Dict[str, List[float]],
) -> List[Dict[str, Any]]:
    """Detect machines with >= _BURST_MIN_COUNT alarms within _BURST_WINDOW_MINUTES."""
    bursts: List[Dict[str, Any]] = []
    window_seconds = _BURST_WINDOW_MINUTES * 60.0

    for machine_id, times in machine_times.items():
        # Sliding window.
        n = len(times)
        for i in range(n):
            # Count alarms in [times[i], times[i] + window_seconds].
            j = i
            while j < n and times[j] - times[i] <= window_seconds:
                j += 1
            count = j - i
            if count >= _BURST_MIN_COUNT:
                bursts.append(
                    {
                        "anomaly_type": "alarm_burst",
                        "machine_id": machine_id,
                        "alarm_count": count,
                        "window_minutes": _BURST_WINDOW_MINUTES,
                        "severity": "HIGH",
                        "description": (
                            f"Machine {machine_id}: {count} alarms within " f"{_BURST_WINDOW_MINUTES:.0f}-minute window"
                        ),
                    }
                )
                # Skip past this burst to avoid duplicate windows.
                break  # only report first burst per machine

    return bursts


class AnomalyDetectionNode(FunctionNode):
    """Detect stoppages, out-of-threshold events, alarm bursts, and quality outliers.

    Input state keys:
        normalized_records: canonical-schema records (from DataNormalizationNode)
        metrics:            OEE and production metrics (from DataNormalizationNode)

    Output state keys (partial dict):
        anomaly_highlights: list of anomaly dicts (no raw employee data)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.INTERNAL

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """Capture the runtime config handed down by the inner graph."""
        self._cfg = domain_config(config)

    def execute(self, state: AgentState) -> Dict[str, Any]:
        normalized_records: List[Dict[str, Any]] = state.get("normalized_records") or []
        metrics: Dict[str, Any] = state.get("metrics") or {}

        # The threshold decides which events become anomalies, so an invalid
        # configured value falls back to the documented default rather than
        # comparing every duration against something non-finite — a NaN
        # threshold compares False everywhere and would report no anomalies at
        # all while the run still succeeded.
        try:
            threshold = finite_in_range(
                self._cfg.get("anomaly_highlight_threshold"),
                field="anomaly_highlight_threshold",
                minimum=0.0,
                maximum=1.0,
                default=_DEFAULT_THRESHOLD,
            )
        except InputRejected:
            logger.warning(
                "AnomalyDetectionNode: configured anomaly_highlight_threshold is invalid; "
                "using the documented default"
            )
            threshold = _DEFAULT_THRESHOLD

        try:
            shift_duration_hours = finite_in_range(
                self._cfg.get("shift_duration_hours"),
                field="shift_duration_hours",
                minimum=0.25,
                maximum=24.0,
                default=_DEFAULT_SHIFT_MINUTES / 60.0,
            )
        except InputRejected:
            logger.warning(
                "AnomalyDetectionNode: configured shift_duration_hours is invalid; " "using the documented default"
            )
            shift_duration_hours = _DEFAULT_SHIFT_MINUTES / 60.0
        shift_duration_minutes = shift_duration_hours * 60.0

        anomalies: List[Dict[str, Any]] = []

        # ------------------------------------------------------------------
        # 1. Downtime / stoppage threshold anomalies
        # ------------------------------------------------------------------
        stoppage_events = [
            rec for rec in normalized_records if str(rec.get("event_type", "")).upper() in _DOWNTIME_EVENT_TYPES
        ]
        for rec in stoppage_events:
            duration = float(rec.get("duration_minutes", 0.0))
            if shift_duration_minutes > 0:
                duration_fraction = duration / shift_duration_minutes
            else:
                duration_fraction = 0.0

            if duration_fraction > threshold:
                anomalies.append(
                    {
                        "anomaly_type": "stoppage",
                        "machine_id": rec.get("machine_id", ""),
                        "event_type": rec.get("event_type", ""),
                        "duration_minutes": round(duration, 2),
                        "duration_fraction": round(duration_fraction, 4),
                        "threshold": threshold,
                        "alarm_code": rec.get("alarm_code"),
                        "alarm_description": rec.get("alarm_description"),
                        "severity": "HIGH" if duration_fraction > threshold * 2 else "MEDIUM",
                        "cause": rec.get("alarm_description") or rec.get("notes", ""),
                        "description": (
                            f"{rec.get('event_type','STOP')} on machine "
                            f"{rec.get('machine_id','')} — "
                            f"{duration:.1f} min "
                            f"({duration_fraction * 100:.1f}% of shift)"
                        ),
                    }
                )

        # ------------------------------------------------------------------
        # 2. Alarm burst detection
        # ------------------------------------------------------------------
        machine_alarm_times = _extract_machine_alarm_times(normalized_records)
        burst_anomalies = _detect_alarm_bursts(machine_alarm_times)
        anomalies.extend(burst_anomalies)

        # ------------------------------------------------------------------
        # 3. Quality outlier — reject_rate above threshold
        # ------------------------------------------------------------------
        reject_rate = float(metrics.get("reject_rate", 0.0))
        if reject_rate > _QUALITY_ANOMALY_THRESHOLD:
            anomalies.append(
                {
                    "anomaly_type": "quality_outlier",
                    "machine_id": "shift_aggregate",
                    "reject_rate": round(reject_rate, 4),
                    "quality_rate": round(metrics.get("quality_rate", 1.0 - reject_rate), 4),
                    "severity": "HIGH" if reject_rate > _QUALITY_ANOMALY_THRESHOLD * 2 else "MEDIUM",
                    "description": (
                        f"Reject rate {reject_rate * 100:.2f}% exceeds "
                        f"quality threshold {_QUALITY_ANOMALY_THRESHOLD * 100:.1f}%"
                    ),
                }
            )

        # ------------------------------------------------------------------
        # 4. OEE below acceptable level (< 0.65 is industry standard warning)
        # ------------------------------------------------------------------
        oee = float(metrics.get("oee", 1.0))
        if oee > 0 and oee < 0.65:
            anomalies.append(
                {
                    "anomaly_type": "low_oee",
                    "machine_id": "shift_aggregate",
                    "oee": round(oee, 4),
                    "severity": "HIGH" if oee < 0.50 else "MEDIUM",
                    "description": (f"Shift OEE {oee * 100:.1f}% is below acceptable threshold (65%)"),
                }
            )

        emit_trace_event(
            "shift_anomalies_detected",
            {
                "anomaly_count": len(anomalies),
                "threshold": threshold,
                "high_severity_count": sum(1 for a in anomalies if a.get("severity") == "HIGH"),
            },
            state,
        )

        logger.info(
            "AnomalyDetectionNode: detected %d anomalies (threshold=%.2f)",
            len(anomalies),
            threshold,
        )

        return {
            "anomaly_highlights": anomalies,
        }
