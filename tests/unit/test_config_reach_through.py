"""Declared runtime values must reach the node that reads them.

The framework invokes a node as ``node(state)`` and calls ``execute(state)``
with no config argument, so configuration can only arrive by construction. A
node that reads its settings from a per-call argument therefore reads nothing,
silently degrades to its built-in defaults, and leaves every declared value in
``config/config.yaml`` inert while the suite stays green.

These tests assert reach-through by OBSERVING A CHANGED OUTPUT rather than by
inspecting an attribute: a value that is stored but never consulted would pass
an attribute check and still be dead.
"""

import copy
import json
from pathlib import Path

import pytest
import yaml
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import MfgC2004Agent

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"

_PAYLOAD = json.dumps(
    {
        "shift_id": "SH-CONFIG-TEST",
        "shift_start_time": "2026-08-31T06:00:00Z",
        "log_data": [
            {
                "timestamp": "2026-08-31T07:00:00Z",
                "event_type": "STOP",
                "machine_id": "M-101",
                "duration_minutes": 120,
            }
        ],
    }
)


def _base_config() -> dict:
    with open(_CONFIG_PATH, encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    return loaded if isinstance(loaded, dict) else {}


def _report_with(**overrides) -> str:
    config = copy.deepcopy(_base_config())
    config.setdefault("mfg_c2_004", {}).update(overrides)
    agent = MfgC2004Agent(config=config)
    agent.compile()
    out = agent.invoke(_PAYLOAD, ctx=InvocationContext(caller_trust_level=TrustLevel.INTERNAL))
    return str(out.get("output"))


class TestShippedConfigIsWellFormed:
    """The values the repo ships are the ones the nodes expect."""

    def test_domain_namespace_is_present(self):
        assert "mfg_c2_004" in _base_config()

    @pytest.mark.parametrize(
        "key",
        [
            "system_prompt",
            "report_template_path",
            "shift_duration_hours",
            "anomaly_highlight_threshold",
            "max_log_records_per_shift",
        ],
    )
    def test_every_declared_key_has_a_reader(self, key):
        # A declared value with no reader is a promise the agent does not keep.
        sources = list(Path(__file__).resolve().parents[2].joinpath("src").rglob("*.py"))
        assert any(
            key in path.read_text(encoding="utf-8") for path in sources
        ), f"config key {key!r} is declared but read nowhere in src/"

    def test_llm_block_has_a_reader(self):
        sources = list(Path(__file__).resolve().parents[2].joinpath("src").rglob("*.py"))
        joined = "\n".join(path.read_text(encoding="utf-8") for path in sources)
        assert "max_tokens" in joined
        assert '"llm"' in joined or "'llm'" in joined


class TestConfigReachesTheInnerGraph:
    """Changing a declared value must change the report."""

    def test_shift_duration_changes_the_computed_availability(self):
        eight_hours = _report_with(shift_duration_hours=8)
        twelve_hours = _report_with(shift_duration_hours=12)
        # 120 minutes of downtime is 25% of an 8-hour shift, 16.7% of a 12-hour one.
        assert "| Availability | 75.0%" in eight_hours
        assert "| Availability | 83.3%" in twelve_hours

    def test_anomaly_threshold_changes_what_is_flagged(self):
        # 120 minutes is 25% of the shift: flagged at 0.15, not at 0.30.
        assert "stoppage" in _report_with(anomaly_highlight_threshold=0.15)
        assert "stoppage" not in _report_with(anomaly_highlight_threshold=0.30)

    def test_record_cap_is_honoured(self):
        config = copy.deepcopy(_base_config())
        config["mfg_c2_004"]["max_log_records_per_shift"] = 1
        agent = MfgC2004Agent(config=config)
        agent.compile()
        payload = json.dumps(
            {
                "shift_id": "SH-CAP-TEST",
                "shift_start_time": "2026-08-31T06:00:00Z",
                "log_data": [
                    {"timestamp": "t", "event_type": "PRODUCTION", "units_produced": 10},
                    {"timestamp": "t", "event_type": "PRODUCTION", "units_produced": 10},
                ],
            }
        )
        out = agent.invoke(payload, ctx=InvocationContext(caller_trust_level=TrustLevel.INTERNAL))
        # Only the first record survives the cap, so the units total is 10.
        assert "10.00" in str(out.get("output"))


class TestInvalidConfigFailsOntoTheDocumentedDefault:
    """A misconfigured value must not disable a comparison silently."""

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "not-a-number", -1])
    def test_invalid_shift_duration_falls_back(self, bad):
        report = _report_with(shift_duration_hours=bad)
        assert "| Availability | 75.0%" in report

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", 5.0])
    def test_invalid_threshold_falls_back_and_still_detects(self, bad):
        # A NaN threshold compares False against every duration, which would
        # report no anomalies at all while the run still succeeded.
        assert "stoppage" in _report_with(anomaly_highlight_threshold=bad)
