"""Report invariants: what the shift report promises about its own numbers.

This agent reports operational quantities — OEE and its component rates, unit
counts, downtime minutes, reject rates. It reports NO monetary values, so the
rounding grid some report-generating agents apply (monetary aggregates snapped
to the nearest 1,000) does not apply here and is deliberately absent.

That absence is pinned rather than assumed. A grid imported into this agent
later would corrupt exactly the values it exists to report: an availability of
0.7512 is a ratio, not an amount, and a machine identifier such as SKF-6205 is
a physical asset reference whose digits are part of its name. Both must survive
byte-identical.

The stated invariant this report DOES carry is fidelity: every number the
report renders is the number the pipeline computed, and every identifier the
report renders is the identifier the caller supplied.
"""

import json
from pathlib import Path

import pytest
import yaml
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import MfgC2004Agent

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


def _config() -> dict:
    with open(_CONFIG_PATH, encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    return loaded if isinstance(loaded, dict) else {}


def _report(log_data: list, shift_id: str = "SH-2026-08-31-A") -> str:
    agent = MfgC2004Agent(config=_config())
    agent.compile()
    payload = json.dumps(
        {
            "shift_id": shift_id,
            "shift_start_time": "2026-08-31T06:00:00Z",
            "log_data": log_data,
        }
    )
    out = agent.invoke(payload, ctx=InvocationContext(caller_trust_level=TrustLevel.INTERNAL))
    assert out.get("status") == "success", out
    return str(out.get("output"))


class TestIdentifiersSurviveByteIdentical:
    """Manufacturing identifiers are asset names, not quantities."""

    @pytest.mark.parametrize(
        "machine_id",
        [
            "SKF-6205",  # the bearing code a rounding grid corrupts to SKF-6,000
            "SKF-6205-2RS",
            "STU-1234",
            "M-101",
            "LINE_02",
            "20260712001",  # pure-numeric asset code
        ],
    )
    def test_machine_identifier_is_rendered_verbatim(self, machine_id):
        report = _report(
            [
                {
                    "timestamp": "2026-08-31T07:00:00Z",
                    "event_type": "STOP",
                    "machine_id": machine_id,
                    "duration_minutes": 120,
                }
            ]
        )
        assert machine_id in report

    @pytest.mark.parametrize(
        "shift_id",
        ["SH-2026-08-31-A", "SHIFT-1234", "S-20260831-001"],
    )
    def test_shift_identifier_is_rendered_verbatim(self, shift_id):
        report = _report(
            [{"timestamp": "2026-08-31T07:00:00Z", "event_type": "PRODUCTION"}],
            shift_id=shift_id,
        )
        assert f"**Shift ID:** {shift_id}" in report


class TestNoRoundingGridIsApplied:
    """No monetary grid exists here, and none may be introduced silently."""

    def test_computed_rates_are_reported_at_full_precision(self):
        # 90 minutes of downtime in a 480-minute shift is an availability of
        # 0.8125 — a grid would round the underlying figure away.
        report = _report(
            [
                {
                    "timestamp": "2026-08-31T07:00:00Z",
                    "event_type": "STOP",
                    "machine_id": "M-101",
                    "duration_minutes": 90,
                }
            ]
        )
        assert "| Availability | 81.2%" in report or "| Availability | 81.3%" in report

    def test_unit_counts_are_not_snapped_to_a_grid(self):
        report = _report(
            [
                {
                    "timestamp": "2026-08-31T07:00:00Z",
                    "event_type": "PRODUCTION",
                    "machine_id": "M-101",
                    "units_produced": 9999,
                    "units_target": 12345,
                    "units_rejected": 7,
                }
            ]
        )
        # A monetary grid would render these as 10,000 and 12,000.
        assert "9999.00" in report
        assert "12345.00" in report
        assert "10,000" not in report
        assert "12,000" not in report

    def test_downtime_minutes_are_not_snapped(self):
        report = _report(
            [
                {
                    "timestamp": "2026-08-31T07:00:00Z",
                    "event_type": "STOP",
                    "machine_id": "M-101",
                    "duration_minutes": 1234,
                }
            ]
        )
        assert "1234.00" in report
        assert "1,000" not in report


class TestRendererCompleteness:
    """The report is a document, never unexpanded template markup."""

    def test_report_contains_no_residual_template_syntax(self):
        report = _report(
            [
                {
                    "timestamp": "2026-08-31T07:00:00Z",
                    "event_type": "STOP",
                    "machine_id": "M-101",
                    "duration_minutes": 120,
                }
            ]
        )
        # The template engine is an optional dependency. Whichever renderer
        # runs, the caller must receive a finished document.
        assert "{{" not in report
        assert "{%" not in report

    def test_every_declared_metric_row_is_rendered(self):
        report = _report(
            [
                {
                    "timestamp": "2026-08-31T07:00:00Z",
                    "event_type": "PRODUCTION",
                    "machine_id": "M-101",
                    "units_produced": 800,
                    "units_target": 1000,
                    "units_rejected": 60,
                }
            ]
        )
        for label in (
            "OEE",
            "Availability",
            "Performance Rate",
            "Quality Rate",
            "Units Produced",
            "Units Target",
            "Units Variance",
            "Downtime (min)",
            "Reject Rate",
        ):
            assert f"| {label} |" in report

    def test_report_reflects_the_caller_data_not_a_baseline(self):
        # A stub-only path would emit the same document for any input.
        busy = _report(
            [
                {
                    "timestamp": "2026-08-31T07:00:00Z",
                    "event_type": "STOP",
                    "machine_id": "M-101",
                    "duration_minutes": 200,
                },
                {
                    "timestamp": "2026-08-31T08:00:00Z",
                    "event_type": "PRODUCTION",
                    "machine_id": "M-101",
                    "units_produced": 500,
                    "units_target": 1000,
                    "units_rejected": 100,
                },
            ]
        )
        quiet = _report(
            [
                {
                    "timestamp": "2026-08-31T07:00:00Z",
                    "event_type": "PRODUCTION",
                    "machine_id": "M-101",
                    "units_produced": 1000,
                    "units_target": 1000,
                    "units_rejected": 0,
                }
            ]
        )
        assert busy != quiet
        assert "No anomalies detected during this shift." in quiet
        assert "No anomalies detected during this shift." not in busy
