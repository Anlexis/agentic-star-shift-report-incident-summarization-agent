"""AgentCore Platform v1.0"""

# MFG-C2-004 — DataNormalizationNode
# Domain node 2: normalize MES log records to canonical schema.
# - Timestamps → ISO-8601 UTC strings
# - Units → SI base units
# - Alarm codes → human-readable descriptions (machine event taxonomy)
# - Compute OEE, availability, performance_rate, quality_rate,
#   units_produced/target/variance, downtime_minutes, reject_rate
#
# Wired by the inner graph (DomainWorkflowGraph).
# Returns only changed state keys (partial dict).

import logging
from datetime import datetime, timezone
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.validation import InputRejected, domain_config, finite_in_range

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Alarm code taxonomy (canonical MES alarm descriptions)
# Production sites use site-specific codes; this table maps common ones.
# Extend via config for site-specific overrides.
# ---------------------------------------------------------------------------
_DEFAULT_ALARM_DESCRIPTIONS: Dict[str, str] = {
    "ALM001": "Machine overheating — thermal shutdown",
    "ALM002": "Feed system jam — material blockage",
    "ALM003": "Spindle speed out of range",
    "ALM004": "Coolant level low",
    "ALM005": "Tool wear limit exceeded",
    "ALM006": "Dimensional tolerance violation",
    "ALM007": "Conveyor stop — downstream jam",
    "ALM008": "Safety interlock triggered",
    "ALM009": "Power fluctuation detected",
    "ALM010": "Sensor calibration drift",
    "STOP001": "Planned maintenance stop",
    "STOP002": "Unplanned downtime — cause unknown",
    "STOP003": "Changeover / setup",
    "QA001": "Inspection failure — surface defect",
    "QA002": "Inspection failure — dimensional out-of-spec",
    "QA003": "Sample rejected — final QC check",
}

# Unit normalisation map: (input_unit) -> (canonical_unit, scale_factor)
_UNIT_CONVERSIONS: Dict[str, Tuple[str, float]] = {
    "mm/min": ("m/min", 0.001),
    "mm/s": ("m/s", 0.001),
    "rpm": ("rpm", 1.0),
    "pcs": ("units", 1.0),
    "pieces": ("units", 1.0),
    "°C": ("°C", 1.0),
    "degC": ("°C", 1.0),
    "bar": ("Pa", 100000.0),
    "kPa": ("Pa", 1000.0),
    "MPa": ("Pa", 1_000_000.0),
    "kg": ("kg", 1.0),
    "g": ("kg", 0.001),
    "kW": ("W", 1000.0),
    "W": ("W", 1.0),
    "l/min": ("m³/min", 0.001),
    "min": ("min", 1.0),
    "s": ("min", 1 / 60.0),
    "h": ("min", 60.0),
}


def _normalize_timestamp(ts: Any) -> str:
    """Convert various timestamp formats to ISO-8601 UTC string."""
    if not ts:
        return ""
    if isinstance(ts, (int, float)):
        # Unix epoch seconds or milliseconds.
        if ts > 1e10:
            ts = ts / 1000.0
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.isoformat()
    ts_str = str(ts).strip()
    # Already ISO-like with timezone.
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(ts_str.replace("Z", "+00:00"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            continue
    # Return as-is if unparseable — still useful downstream.
    return ts_str


def _normalize_unit_value(value: Any, unit: Optional[str]) -> Tuple[Any, Optional[str]]:
    """Convert value + unit to canonical form."""
    if unit is None or not isinstance(value, (int, float)):
        return value, unit
    mapping = _UNIT_CONVERSIONS.get(unit)
    if mapping is None:
        return value, unit
    canonical_unit, scale = mapping
    return round(value * scale, 6), canonical_unit


def _resolve_alarm_code(code: str, custom_map: Dict[str, str]) -> str:
    """Return human-readable description for an alarm/event code."""
    return custom_map.get(code) or _DEFAULT_ALARM_DESCRIPTIONS.get(code) or code


def _compute_oee(metrics_raw: Dict[str, Any], shift_duration_minutes: float) -> Dict[str, Any]:
    """Compute OEE and component metrics from raw counters.

    OEE = Availability × Performance × Quality
    - Availability   = (planned_time - downtime) / planned_time
    - Performance    = (units_produced / cycle_time) / (planned_time - downtime)
                       simplified: actual_output / ideal_output
    - Quality        = good_units / units_produced
    """
    if shift_duration_minutes <= 0:
        return {}

    planned_minutes = shift_duration_minutes
    downtime_minutes = float(metrics_raw.get("downtime_minutes", 0.0))
    units_produced = float(metrics_raw.get("units_produced", 0.0))
    units_target = float(metrics_raw.get("units_target", 0.0))
    units_rejected = float(metrics_raw.get("units_rejected", 0.0))

    operating_minutes = max(planned_minutes - downtime_minutes, 0.0)

    availability = operating_minutes / planned_minutes if planned_minutes > 0 else 0.0
    performance = units_produced / units_target if units_target > 0 else (1.0 if units_produced == 0 else 0.0)
    good_units = max(units_produced - units_rejected, 0.0)
    quality = good_units / units_produced if units_produced > 0 else 1.0
    oee = availability * performance * quality

    reject_rate = units_rejected / units_produced if units_produced > 0 else 0.0
    units_variance = units_produced - units_target

    return {
        "oee": round(oee, 4),
        "availability": round(availability, 4),
        "performance_rate": round(performance, 4),
        "quality_rate": round(quality, 4),
        "units_produced": units_produced,
        "units_target": units_target,
        "units_variance": round(units_variance, 2),
        "downtime_minutes": round(downtime_minutes, 2),
        "reject_rate": round(reject_rate, 4),
        "good_units": round(good_units, 2),
    }


class DataNormalizationNode(FunctionNode):
    """Normalize MES log records to canonical schema and compute OEE metrics.

    Input state keys:
        raw_log_records: list of PII-free log entries (from LogIngestionNode)

    Output state keys (partial dict):
        normalized_records: canonical-schema records
        metrics:            OEE + production metrics dict
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.INTERNAL

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """Capture the runtime config handed down by the inner graph."""
        self._cfg = domain_config(config)

    def execute(self, state: AgentState) -> Dict[str, Any]:
        raw_records: List[Dict[str, Any]] = state.get("raw_log_records") or []

        # Shift duration drives every percentage-based metric, so an invalid
        # configured value fails closed onto the documented default rather than
        # silently producing ratios against a nonsense denominator.
        try:
            shift_duration_hours = finite_in_range(
                self._cfg.get("shift_duration_hours"),
                field="shift_duration_hours",
                minimum=0.25,
                maximum=24.0,
                default=8.0,
            )
        except InputRejected:
            logger.warning(
                "DataNormalizationNode: configured shift_duration_hours is invalid; "
                "using the documented default of 8 hours"
            )
            shift_duration_hours = 8.0
        shift_duration_minutes = shift_duration_hours * 60.0

        configured_alarm_map = self._cfg.get("alarm_code_map")
        custom_alarm_map: Dict[str, str] = configured_alarm_map if isinstance(configured_alarm_map, dict) else {}

        normalized: List[Dict[str, Any]] = []

        # Accumulators for OEE computation.
        downtime_minutes_total = 0.0
        units_produced_total = 0.0
        units_target_total = 0.0
        units_rejected_total = 0.0

        for record in raw_records:
            norm: Dict[str, Any] = {}

            # 1. Normalize timestamp.
            norm["timestamp"] = _normalize_timestamp(record.get("timestamp"))

            # 2. Copy / normalize event_type.
            event_type = str(record.get("event_type", "")).upper()
            norm["event_type"] = event_type

            # 3. Normalize machine_id (no PII involved — machine identifiers kept as-is).
            norm["machine_id"] = record.get("machine_id", "")

            # 4. Resolve alarm / event code to description.
            code = record.get("alarm_code") or record.get("event_code", "")
            if code:
                norm["alarm_code"] = str(code).upper()
                norm["alarm_description"] = _resolve_alarm_code(str(code).upper(), custom_alarm_map)

            # 5. Normalize numeric value + unit.
            raw_value = record.get("value")
            raw_unit = record.get("unit")
            norm_value, norm_unit = _normalize_unit_value(raw_value, raw_unit)
            if raw_value is not None:
                norm["value"] = norm_value
            if norm_unit is not None:
                norm["unit"] = norm_unit

            # 6. Carry over duration_minutes (already in minutes if from alarm events).
            duration_raw = record.get("duration_minutes") or record.get("duration_min", 0.0)
            if duration_raw:
                duration_val, _ = _normalize_unit_value(float(duration_raw), "min")
                norm["duration_minutes"] = round(float(duration_val), 2)
                if event_type in {"STOP", "DOWNTIME", "ALARM"}:
                    downtime_minutes_total += float(duration_val)
            else:
                norm["duration_minutes"] = 0.0

            # 7. Accumulate production counters.
            if event_type == "PRODUCTION":
                units_produced_total += float(record.get("units_produced", 0))
                units_target_total += float(record.get("units_target", 0))
                units_rejected_total += float(record.get("units_rejected", 0))

            # 8. Carry over any additional non-PII fields.
            for extra_key in ("severity", "line_id", "product_id", "shift_id", "notes"):
                if extra_key in record:
                    val = record[extra_key]
                    norm[extra_key] = val if not isinstance(val, str) else val[:500]

            normalized.append(norm)

        # Compute OEE metrics.
        raw_metric_accumulators = {
            "downtime_minutes": downtime_minutes_total,
            "units_produced": units_produced_total,
            "units_target": units_target_total,
            "units_rejected": units_rejected_total,
        }
        metrics = _compute_oee(raw_metric_accumulators, shift_duration_minutes)
        metrics["shift_duration_minutes"] = shift_duration_minutes

        emit_trace_event(
            "shift_metrics_computed",
            {
                "records_normalized": len(normalized),
                "oee": metrics.get("oee"),
                "shift_duration_minutes": shift_duration_minutes,
            },
            state,
        )

        logger.info(
            "DataNormalizationNode: normalized %d records; OEE=%.3f",
            len(normalized),
            metrics.get("oee", 0.0),
        )

        return {
            "normalized_records": normalized,
            "metrics": metrics,
        }
