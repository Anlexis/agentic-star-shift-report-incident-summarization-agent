"""Proof-of-Boundary test PB-03 — MFG-C2-004

PB-03: Malformed log schema injection
    Pass structurally invalid MES log payloads to LogIngestionNode and assert:
    - input validation rejects the payload with an appropriate error message
    - raw_log_records is empty (no partial processing of bad data)
    - No unhandled exception escapes (fail-safe, not fail-silent)

Sub-cases covered:
    PB-03a: Payload is a plain string (not a JSON object)
    PB-03b: Payload is a JSON object but missing shift_id (required field)
    PB-03c: Payload is a JSON object but missing log_data (required field)
    PB-03d: log_data is not a list (bare string instead of list)
    PB-03e: Individual log entry missing event_type (required per-record field)
"""

import json
import sys
import os

import pytest

# Allow running from the repo root (adds project root to path for framework stubs).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.nodes.log_ingestion_node import LogIngestionNode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(validated_input: str) -> dict:
    """Build a minimal state dict for LogIngestionNode testing."""
    return {
        "user_input": validated_input,
        "validated_input": validated_input,
        "raw_log_records": None,
        "log_ingestion_errors": None,
        "shift_report": None,
        "normalized_records": None,
        "anomaly_highlights": None,
        "metrics": None,
        "summary_text": None,
        "trace_id": None,
        "correlation_id": None,
        "node_history": [],
    }


def _run_ingestion(raw_input: str) -> dict:
    """Run LogIngestionNode.execute() and return the partial-dict output."""
    node = LogIngestionNode()
    state = _make_state(raw_input)
    return node.execute(state)


# ---------------------------------------------------------------------------
# PB-03: Malformed Log Schema Injection
# ---------------------------------------------------------------------------


class TestPB03MalformedLogSchemaRejection:
    """PB-03: Structurally invalid MES log payloads must be rejected on ingest.

    All sub-cases assert:
    1. raw_log_records is empty (no partial processing)
    2. log_ingestion_errors is non-empty (clear rejection message)
    3. No unhandled exception (assert statements below implicitly verify this)
    """

    def test_pb03a_plain_string_payload_rejected(self):
        """PB-03a: A plain non-JSON string (not a JSON object) is rejected."""
        result = _run_ingestion("this is not valid JSON or a JSON object")

        assert result.get("raw_log_records") == [], (
            "raw_log_records must be empty when payload is not a JSON object. " f"Got: {result.get('raw_log_records')}"
        )

        errors = result.get("log_ingestion_errors") or []
        assert len(errors) > 0, "log_ingestion_errors must be non-empty on rejection"

        # The error must name the validation that refused the payload
        error_text = " ".join(errors).lower()
        assert (
            "input validation failed" in error_text or "json object" in error_text
        ), f"Input-rejection message not found in errors: {errors}"

    def test_pb03b_missing_shift_id_rejected(self):
        """PB-03b: Payload missing required field shift_id is rejected."""
        payload = json.dumps(
            {
                # shift_id intentionally omitted
                "shift_start_time": "2026-06-17T06:00:00Z",
                "log_data": [
                    {
                        "timestamp": "2026-06-17T06:00:00Z",
                        "event_type": "PRODUCTION",
                        "machine_id": "MC-01",
                        "units_produced": 100,
                    }
                ],
            }
        )
        result = _run_ingestion(payload)

        assert result.get("raw_log_records") == [], (
            "raw_log_records must be empty when shift_id is missing. " f"Got: {result.get('raw_log_records')}"
        )

        errors = result.get("log_ingestion_errors") or []
        assert len(errors) > 0, "log_ingestion_errors must be non-empty on rejection"

        error_text = " ".join(errors).lower()
        assert (
            "missing" in error_text or "required" in error_text or "shift_id" in error_text
        ), f"Expected 'missing'/'required'/'shift_id' in error message: {errors}"

    def test_pb03c_missing_log_data_rejected(self):
        """PB-03c: Payload missing required field log_data is rejected."""
        payload = json.dumps(
            {
                "shift_id": "SHIFT-MALFORMED",
                "shift_start_time": "2026-06-17T06:00:00Z",
                # log_data intentionally omitted
            }
        )
        result = _run_ingestion(payload)

        assert result.get("raw_log_records") == [], (
            "raw_log_records must be empty when log_data is missing. " f"Got: {result.get('raw_log_records')}"
        )

        errors = result.get("log_ingestion_errors") or []
        assert len(errors) > 0, "log_ingestion_errors must be non-empty on rejection"

        error_text = " ".join(errors).lower()
        assert (
            "missing" in error_text or "required" in error_text or "log_data" in error_text
        ), f"Expected 'missing'/'required'/'log_data' in error message: {errors}"

    def test_pb03d_log_data_not_a_list_rejected(self):
        """PB-03d: log_data is a bare string (not a list) is rejected."""
        payload = json.dumps(
            {
                "shift_id": "SHIFT-MALFORMED",
                "shift_start_time": "2026-06-17T06:00:00Z",
                "log_data": "this should be a list not a string",
            }
        )
        result = _run_ingestion(payload)

        assert result.get("raw_log_records") == [], (
            "raw_log_records must be empty when log_data is not a list. " f"Got: {result.get('raw_log_records')}"
        )

        errors = result.get("log_ingestion_errors") or []
        assert len(errors) > 0, "log_ingestion_errors must be non-empty on rejection"

        error_text = " ".join(errors).lower()
        assert (
            "list" in error_text or "log_data" in error_text
        ), f"Expected 'list' or 'log_data' in error message: {errors}"

    def test_pb03e_missing_event_type_in_record_skipped_not_silently_ignored(self):
        """PB-03e: Log entry missing required event_type is skipped with a per-record error.

        A partial payload with one valid record and one invalid record (missing event_type):
        - The valid record is processed
        - The invalid record is skipped
        - log_ingestion_errors is non-empty (skip reason recorded — no silent failure)
        """
        payload = json.dumps(
            {
                "shift_id": "SHIFT-PARTIAL",
                "shift_start_time": "2026-06-17T06:00:00Z",
                "log_data": [
                    # Valid record
                    {
                        "timestamp": "2026-06-17T06:00:00Z",
                        "event_type": "PRODUCTION",
                        "machine_id": "MC-01",
                        "units_produced": 50,
                    },
                    # Invalid record: missing event_type (required per-record field)
                    {"timestamp": "2026-06-17T07:00:00Z", "machine_id": "MC-02"},
                ],
            }
        )
        result = _run_ingestion(payload)

        # The valid record should be processed
        raw_records = result.get("raw_log_records") or []
        assert len(raw_records) == 1, (
            f"Expected 1 valid record processed, got {len(raw_records)}. " f"raw_log_records: {raw_records}"
        )

        # The invalid record's skip must be recorded — no silent failure
        errors = result.get("log_ingestion_errors") or []
        assert len(errors) > 0, (
            "log_ingestion_errors must be non-empty when a record is skipped. " "No silent failures allowed."
        )

        error_text = " ".join(errors).lower()
        assert (
            "skip" in error_text or "missing" in error_text or "event_type" in error_text
        ), f"Expected skip/missing/event_type mention in errors: {errors}"

    def test_no_exception_escapes_on_malformed_input(self):
        """No unhandled exception must escape LogIngestionNode on any malformed input."""
        malformed_inputs = [
            "",  # empty string
            "null",  # JSON null
            "[]",  # JSON array (not object)
            "42",  # JSON number
            "{invalid json",  # unparseable
            json.dumps({"shift_id": "X", "shift_start_time": "Y"}),  # missing log_data
            json.dumps({"shift_id": "X", "log_data": None, "shift_start_time": "Y"}),
        ]
        node = LogIngestionNode()
        for raw in malformed_inputs:
            try:
                state = _make_state(raw)
                out = node.execute(state)
                # Must return a dict with raw_log_records (may be empty list)
                assert isinstance(out, dict), f"execute() must return a dict for input={raw!r}, got {type(out)}"
                assert "raw_log_records" in out, f"raw_log_records key must be present in output for input={raw!r}"
            except Exception as exc:
                pytest.fail(f"Unhandled exception for malformed input {raw!r}: {type(exc).__name__}: {exc}")
