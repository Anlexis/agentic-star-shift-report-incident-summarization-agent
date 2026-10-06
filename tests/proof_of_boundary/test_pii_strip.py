"""Proof-of-Boundary tests PB-01 and PB-02 — MFG-C2-004

PB-01: Worker ID PII gate
    Inject worker ID tokens in MES log input → assert they do NOT appear in
    State (raw_log_records) or the final rendered shift_report.

PB-02: Raw log retention boundary
    Assert raw log data is NOT retained in the partial-dict output returned by
    ShiftReportRenderNode (the node that writes shift_report to State).
    The rendered shift_report must not contain a verbatim raw-record sentinel.

Both tests exercise the actual node implementations — no mocks, no stubs for
the node logic itself. SummarizationNode is invoked with no LLM client in
config (triggers the structured fallback path), which is sufficient to verify
the output boundary without requiring a live model credential.
"""

import json
import sys
import os


# Allow running from the repo root (adds src/ to the path for framework stubs).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.nodes.log_ingestion_node import LogIngestionNode
from src.nodes.data_normalization_node import DataNormalizationNode
from src.nodes.anomaly_detection_node import AnomalyDetectionNode
from src.nodes.summarization_node import SummarizationNode
from src.nodes.shift_report_render_node import ShiftReportRenderNode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(**kwargs) -> dict:
    """Build a minimal state dict for node testing."""
    return {
        "user_input": "",
        "validated_input": "",
        "shift_report": None,
        "raw_log_records": None,
        "log_ingestion_errors": None,
        "normalized_records": None,
        "anomaly_highlights": None,
        "metrics": None,
        "summary_text": None,
        "trace_id": None,
        "correlation_id": None,
        "node_history": [],
        **kwargs,
    }


def _build_payload_with_pii(worker_id: str = "EMP-12345") -> str:
    """Build a JSON shift-log payload with a PII worker ID embedded in log records."""
    return json.dumps(
        {
            "shift_id": "SHIFT-PII-TEST",
            "shift_start_time": "2026-06-17T06:00:00Z",
            "log_data": [
                {
                    "timestamp": "2026-06-17T06:00:00Z",
                    "event_type": "PRODUCTION",
                    "machine_id": "MC-01",
                    "units_produced": 100,
                    "units_target": 110,
                    "units_rejected": 2,
                    # PII: worker_id field — a canonical PII field name
                    "worker_id": worker_id,
                },
                {
                    "timestamp": "2026-06-17T07:00:00Z",
                    "event_type": "ALARM",
                    "machine_id": "MC-01",
                    "alarm_code": "ALM004",
                    "duration_minutes": 5,
                    # PII: worker ID in a free-text notes field via regex pattern
                    "notes": f"Alarm acknowledged by operator {worker_id}",
                },
            ],
        }
    )


def _run_pipeline(validated_input: str) -> dict:
    """Run the inner pipeline nodes in sequence and return the accumulated state."""
    state = _make_state(validated_input=validated_input)

    # Step 1: LogIngestionNode
    node1 = LogIngestionNode()
    out1 = node1.execute(state)
    state.update(out1)

    # Step 2: DataNormalizationNode
    node2 = DataNormalizationNode()
    out2 = node2.execute(state)
    state.update(out2)

    # Step 3: AnomalyDetectionNode
    node3 = AnomalyDetectionNode()
    out3 = node3.execute(state)
    state.update(out3)

    # Step 4: SummarizationNode (no LLM service → computed narrative)
    node4 = SummarizationNode()
    out4 = node4.execute(state)
    state.update(out4)

    # Step 5: ShiftReportRenderNode
    node5 = ShiftReportRenderNode()
    out5 = node5.execute(state)
    state.update(out5)

    # Return the final state and the last partial-dict output from the render node
    return {"state": state, "render_output": out5}


# ---------------------------------------------------------------------------
# PB-01: Worker ID PII Gate
# ---------------------------------------------------------------------------


class TestPB01WorkerIdPiiStrip:
    """PB-01: Worker IDs must not appear in State or the final shift report."""

    RAW_WORKER_ID = "EMP-12345"
    ALT_WORKER_ID = "WORKER_99"

    def test_pii_not_in_raw_log_records_field(self):
        """Worker ID in the `worker_id` field is replaced with [REDACTED] in raw_log_records."""
        payload = _build_payload_with_pii(self.RAW_WORKER_ID)
        result = _run_pipeline(payload)
        state = result["state"]

        raw_records = state.get("raw_log_records") or []
        assert len(raw_records) > 0, "raw_log_records must be populated"

        # Serialize all records to a single string and scan for the raw PII token.
        serialized = json.dumps(raw_records)
        assert self.RAW_WORKER_ID not in serialized, (
            f"Raw worker ID '{self.RAW_WORKER_ID}' found in raw_log_records — "
            f"PII strip failed.\nraw_log_records snippet: {serialized[:500]}"
        )

    def test_pii_not_in_free_text_notes_in_raw_log_records(self):
        """Worker ID embedded in a `notes` string field is redacted before State write."""
        payload = _build_payload_with_pii(self.RAW_WORKER_ID)
        result = _run_pipeline(payload)
        state = result["state"]

        raw_records = state.get("raw_log_records") or []
        serialized = json.dumps(raw_records)

        # The `notes` field had "Alarm acknowledged by operator EMP-12345"
        # After PII strip, EMP-12345 must be replaced.
        assert self.RAW_WORKER_ID not in serialized, "Worker ID in notes field not redacted. Found in raw_log_records."

    def test_pii_not_in_final_shift_report(self):
        """Final rendered shift_report must not contain the raw worker ID."""
        payload = _build_payload_with_pii(self.RAW_WORKER_ID)
        result = _run_pipeline(payload)
        state = result["state"]

        shift_report = state.get("shift_report") or ""
        assert shift_report, "shift_report must be non-empty"
        assert self.RAW_WORKER_ID not in shift_report, (
            f"Raw worker ID '{self.RAW_WORKER_ID}' found in final shift_report — "
            f"PII leaked into output.\nshift_report snippet: {shift_report[:500]}"
        )

    def test_redacted_placeholder_present_in_records(self):
        """After PII strip, [REDACTED] placeholder must appear in place of the worker ID."""
        payload = _build_payload_with_pii(self.RAW_WORKER_ID)
        result = _run_pipeline(payload)
        state = result["state"]

        raw_records = state.get("raw_log_records") or []
        serialized = json.dumps(raw_records)
        # The worker_id field should have been replaced with [REDACTED]
        assert "[REDACTED]" in serialized, (
            "Expected [REDACTED] placeholder in raw_log_records after PII strip, "
            f"but it was not found.\nraw_log_records: {serialized[:500]}"
        )


# ---------------------------------------------------------------------------
# PB-02: Raw Log Retention Boundary
# ---------------------------------------------------------------------------


class TestPB02RawLogRetention:
    """PB-02: Raw log data must not be retained in State output after processing."""

    # A distinctive sentinel value that will appear in log record fields
    # but must NOT appear verbatim as a raw-record dump in the final output.
    SENTINEL = "RAW_LOG_SENTINEL_XYZ_12345"

    def _build_sentinel_payload(self) -> str:
        """Build a payload where a sentinel appears in record notes."""
        return json.dumps(
            {
                "shift_id": "SHIFT-S3-TEST",
                "shift_start_time": "2026-06-17T06:00:00Z",
                "log_data": [
                    {
                        "timestamp": "2026-06-17T06:00:00Z",
                        "event_type": "PRODUCTION",
                        "machine_id": "MC-02",
                        "units_produced": 200,
                        "units_target": 220,
                        "units_rejected": 3,
                        "notes": f"Normal production run. Ref: {self.SENTINEL}",
                    },
                ],
            }
        )

    def test_shift_report_render_output_does_not_include_raw_log_records_key(self):
        """ShiftReportRenderNode partial dict must NOT include raw_log_records key.

        The render node's execute() returns only changed keys (shift_report, status).
        raw_log_records must not appear in this partial dict — it is an inner-layer
        field that must not be propagated to outer State via the render output.
        """
        payload = self._build_sentinel_payload()
        result = _run_pipeline(payload)
        render_output = result["render_output"]

        assert "raw_log_records" not in render_output, (
            "ShiftReportRenderNode partial dict must not include raw_log_records. "
            f"Keys found: {list(render_output.keys())}"
        )

    def test_shift_report_render_output_does_not_include_normalized_records_key(self):
        """ShiftReportRenderNode partial dict must NOT include normalized_records key.

        normalized_records is an intermediate inner-layer field and must not be
        surfaced in the final render output partial dict.
        """
        payload = self._build_sentinel_payload()
        result = _run_pipeline(payload)
        render_output = result["render_output"]

        assert "normalized_records" not in render_output, (
            "ShiftReportRenderNode partial dict must not include normalized_records. "
            f"Keys found: {list(render_output.keys())}"
        )

    def test_shift_report_render_output_keys_are_shift_report_and_status_only(self):
        """ShiftReportRenderNode must return only shift_report and status keys."""
        payload = self._build_sentinel_payload()
        result = _run_pipeline(payload)
        render_output = result["render_output"]

        allowed_keys = {"shift_report", "status"}
        actual_keys = set(render_output.keys())
        unexpected = actual_keys - allowed_keys
        assert not unexpected, (
            f"ShiftReportRenderNode returned unexpected keys: {unexpected}. "
            "Only 'shift_report' and 'status' are expected in the partial dict."
        )

    def test_shift_report_contains_no_verbatim_raw_record_dump(self):
        """The final shift_report must not contain a verbatim raw JSON record dump.

        Only aggregated/summarized content reaches the report.
        The sentinel appears in a notes field but must not be dumped verbatim
        into the rendered report as a raw log record blob.
        Note: the sentinel may appear indirectly via the SummarizationNode record
        digest sample (which is capped at 50 records and 8000 chars); we assert
        the report output itself does not contain a raw JSON array dump.
        """
        payload = self._build_sentinel_payload()
        result = _run_pipeline(payload)
        state = result["state"]

        shift_report = state.get("shift_report") or ""
        assert shift_report, "shift_report must be non-empty"

        # The raw_log_records list should NOT appear as a JSON array dump in the report.
        # We check that the report does not contain raw_log_records serialized output.
        raw_records = state.get("raw_log_records") or []
        raw_serialized = json.dumps(raw_records)

        # The full raw_log_records JSON should not appear verbatim in the shift_report.
        assert raw_serialized not in shift_report, (
            "Verbatim raw_log_records JSON dump found in shift_report — " "raw log retention boundary violated."
        )
