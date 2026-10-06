"""AgentCore Platform v1.0"""

# MFG-C2-004 — LogIngestionNode
# Domain node 1: parse the raw MES shift-log payload, validate every caller
# field against explicit bounds, and strip worker identifiers before anything
# is written to State.
#
# Returns only changed state keys (partial dict).

import json
import logging
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.pii import strip_pii_mapping, strip_pii_text
from src.validation import (
    MAX_LOG_RECORDS_CEILING,
    MAX_PAYLOAD_BYTES,
    InputRejected,
    bounded_note,
    domain_config,
    finite_in_range,
    inert_identifier,
    screen_structure,
    screen_text,
)

logger = logging.getLogger(__name__)

# Required top-level keys in the shift log payload.
_REQUIRED_PAYLOAD_KEYS = {"shift_id", "log_data", "shift_start_time"}

# Required keys for each log entry (minimal schema).
_REQUIRED_ENTRY_KEYS = {"timestamp", "event_type"}

# Identifier fields echoed into the shift report; each is locked to the inert
# render alphabet so a caller cannot inject markup or directives through them.
_IDENTIFIER_FIELDS = ("machine_id", "line_id", "product_id", "shift_id")

# Numeric fields a caller may supply per log entry, with their accepted ranges.
# A duration cannot be negative and cannot exceed a month of wall-clock time; a
# unit count cannot be negative. Every bound is explicit — an unbounded numeric
# is the same defect as an unvalidated one.
_NUMERIC_ENTRY_FIELDS: Tuple[Tuple[str, float, float], ...] = (
    ("duration_minutes", 0.0, 44_640.0),
    ("units_produced", 0.0, 1e9),
    ("units_target", 0.0, 1e9),
    ("units_rejected", 0.0, 1e9),
)

# Event-type vocabulary is closed: an unrecognised event type is carried as
# UNKNOWN rather than echoed, so the value cannot reach the report as free text.
_KNOWN_EVENT_TYPES = {
    "PRODUCTION",
    "STOP",
    "DOWNTIME",
    "ALARM",
    "STOPPAGE",
    "FAULT",
    "ERROR",
    "MAINTENANCE",
    "CHANGEOVER",
    "QA",
    "INSPECTION",
}

_KNOWN_SEVERITIES = {"LOW", "MEDIUM", "HIGH", "CRITICAL", "INFO", "WARNING"}


def _parse_payload(validated_input: Any) -> Any:
    """Parse validated_input as JSON when it arrives as a string."""
    if isinstance(validated_input, str):
        try:
            return json.loads(validated_input)
        except (json.JSONDecodeError, ValueError):
            # Not JSON; the caller handles validation of the raw form.
            return validated_input
    return validated_input


class LogIngestionNode(FunctionNode):
    """Parse and validate the MES shift-log payload; strip PII before State write.

    Input state keys:
        validated_input: shift payload accepted by PreProcessNode

    Output state keys (partial dict):
        raw_log_records:      list of validated, PII-free log entries
        log_ingestion_errors: reasons for rejected records (field names only)
        shift_id:             the validated shift identifier
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.INTERNAL

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """Capture the runtime config handed down by the inner graph."""
        self._cfg = domain_config(config)

    def execute(self, state: AgentState) -> Dict[str, Any]:
        validated_input = state.get("validated_input") or state.get("user_input", "")

        ingestion_errors: List[str] = []

        # Size cap before parsing — an oversized payload is refused, not truncated.
        if isinstance(validated_input, str) and len(validated_input.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            emit_trace_event(
                "shift_log_rejected",
                {"reason": "payload_too_large", "limit_bytes": MAX_PAYLOAD_BYTES},
                state,
            )
            return {
                "raw_log_records": [],
                "log_ingestion_errors": [f"Input validation failed: payload exceeds {MAX_PAYLOAD_BYTES} bytes"],
            }

        payload = _parse_payload(validated_input)

        if not isinstance(payload, dict):
            logger.warning("LogIngestionNode: payload is not a mapping (type=%s)", type(payload).__name__)
            emit_trace_event(
                "shift_log_rejected",
                {"reason": "payload_not_a_mapping", "payload_type": type(payload).__name__},
                state,
            )
            return {
                "raw_log_records": [],
                "log_ingestion_errors": [
                    f"Input validation failed: payload must be a JSON object, " f"got {type(payload).__name__}"
                ],
            }

        # Screen the parsed payload depth-first, keys included, before any value
        # is read. Screening after the parse is what makes \u-escaped payloads
        # visible: the escapes are already decoded by this point.
        try:
            screen_structure(payload, field="shift_log")
        except InputRejected as exc:
            emit_trace_event(
                "shift_log_rejected",
                {"reason": "injection_screen", "field": exc.field},
                state,
            )
            return {
                "raw_log_records": [],
                "log_ingestion_errors": [f"Input validation failed: {exc}"],
            }

        missing_keys = _REQUIRED_PAYLOAD_KEYS - payload.keys()
        if missing_keys:
            logger.warning("LogIngestionNode: missing required payload keys")
            emit_trace_event(
                "shift_log_rejected",
                {"reason": "missing_required_fields", "fields": sorted(missing_keys)},
                state,
            )
            return {
                "raw_log_records": [],
                "log_ingestion_errors": [
                    f"Input validation failed: missing required fields: " f"{sorted(missing_keys)}"
                ],
            }

        # The shift identifier is echoed into the report header.
        try:
            shift_id = inert_identifier(payload.get("shift_id"), field="shift_id", required=True)
        except InputRejected as exc:
            emit_trace_event("shift_log_rejected", {"reason": "invalid_identifier", "field": exc.field}, state)
            return {
                "raw_log_records": [],
                "log_ingestion_errors": [f"Input validation failed: {exc}"],
            }

        raw_entries = payload.get("log_data", [])
        if not isinstance(raw_entries, list):
            if isinstance(raw_entries, dict):
                raw_entries = [raw_entries]
            else:
                emit_trace_event("shift_log_rejected", {"reason": "log_data_not_a_list"}, state)
                return {
                    "raw_log_records": [],
                    "log_ingestion_errors": ["Input validation failed: log_data must be a list"],
                    "shift_id": shift_id,
                }

        # Record cap: the configured value is honoured but can never exceed the
        # module ceiling, so a misconfigured value cannot lift the bound.
        try:
            configured_max = int(
                finite_in_range(
                    self._cfg.get("max_log_records_per_shift"),
                    field="max_log_records_per_shift",
                    minimum=1.0,
                    maximum=float(MAX_LOG_RECORDS_CEILING),
                    default=5000.0,
                )
            )
        except InputRejected:
            logger.warning(
                "LogIngestionNode: configured max_log_records_per_shift is invalid; " "using the built-in ceiling"
            )
            configured_max = 5000
        max_records = min(configured_max, MAX_LOG_RECORDS_CEILING)

        if len(raw_entries) > max_records:
            ingestion_errors.append(
                f"Log truncated: {len(raw_entries)} entries exceeded the "
                f"per-shift maximum of {max_records}; only the first "
                f"{max_records} records were processed."
            )
            raw_entries = raw_entries[:max_records]

        cleaned_records: List[Dict[str, Any]] = []
        rejected = 0

        for idx, entry in enumerate(raw_entries):
            if not isinstance(entry, dict):
                ingestion_errors.append(f"Record {idx}: skipped — expected an object, got {type(entry).__name__}")
                rejected += 1
                continue

            missing_entry_keys = _REQUIRED_ENTRY_KEYS - entry.keys()
            if missing_entry_keys:
                ingestion_errors.append(
                    f"Record {idx}: skipped — missing required fields " f"{sorted(missing_entry_keys)}"
                )
                rejected += 1
                continue

            try:
                cleaned_records.append(self._validate_entry(entry, shift_id))
            except InputRejected as exc:
                # Name the field, never the value.
                ingestion_errors.append(f"Record {idx}: skipped — {exc}")
                rejected += 1

        emit_trace_event(
            "shift_log_ingested",
            {
                "records_accepted": len(cleaned_records),
                "records_rejected": rejected,
                "shift_id": shift_id,
            },
            state,
        )

        logger.info(
            "LogIngestionNode: ingested %d records, rejected %d",
            len(cleaned_records),
            rejected,
        )

        return {
            "raw_log_records": cleaned_records,
            "log_ingestion_errors": ingestion_errors,
            "shift_id": shift_id,
        }

    def _validate_entry(self, entry: Dict[str, Any], shift_id: str) -> Dict[str, Any]:
        """Validate one log entry, returning a record safe to store and render.

        Raises InputRejected (naming the field) if any value fails its bound.
        """
        record: Dict[str, Any] = {}

        # Timestamps are carried as opaque text but screened and length-capped.
        record["timestamp"] = screen_text(entry.get("timestamp"), field="timestamp")[:64]

        event_type = str(entry.get("event_type", "")).upper().strip()
        record["event_type"] = event_type if event_type in _KNOWN_EVENT_TYPES else "UNKNOWN"

        for field in _IDENTIFIER_FIELDS:
            if field in entry:
                record[field] = inert_identifier(entry.get(field), field=field)
        record.setdefault("shift_id", shift_id)

        code = entry.get("alarm_code") or entry.get("event_code")
        if code:
            record["alarm_code"] = inert_identifier(str(code).upper(), field="alarm_code")

        if "value" in entry and entry.get("value") is not None:
            record["value"] = finite_in_range(
                entry.get("value"),
                field="value",
                minimum=-1e9,
                maximum=1e9,
            )
        if "unit" in entry and entry.get("unit") is not None:
            record["unit"] = inert_identifier(
                str(entry.get("unit")).replace("/", "_").replace("³", "3").replace("°", ""),
                field="unit",
            )

        for field, minimum, maximum in _NUMERIC_ENTRY_FIELDS:
            if field in entry and entry.get(field) is not None:
                record[field] = finite_in_range(entry.get(field), field=field, minimum=minimum, maximum=maximum)

        severity = str(entry.get("severity", "")).upper().strip()
        if severity:
            record["severity"] = severity if severity in _KNOWN_SEVERITIES else "UNKNOWN"

        if entry.get("notes") is not None:
            record["notes"] = strip_pii_text(bounded_note(entry.get("notes"), field="notes"))

        # Final PII sweep across every retained string value.
        return strip_pii_mapping(record)
