"""Output boundary: the shift report is released only when it passes every check.

The framework resolves an agent's output as
``state.get("formatted_output") or state.get("result")`` with no status check.
Two consequences drive these tests:

  * returning an error status while leaving ``result`` populated still ships the
    ungated report inside the error envelope, so a refusal must CLEAR the
    output-bearing fields;
  * a falsy ``formatted_output`` re-activates that fallback, so the withheld
    notice must be truthy.

The fault is injected on the DATA path — a transport returning a narrative that
echoes a worker identifier — never by patching the gate. Patching the gate would
test the patch rather than the pipeline.

Worker identifiers are the reachable case here: the framework's own output scan
covers credential shapes, so a credential never reaches this node from an inner
FunctionNode. It does not scan for personal data, which is precisely the gap
this boundary closes.
"""

import json
from pathlib import Path

import pytest
import yaml
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import MfgC2004Agent
from src.nodes.post_process_node import WITHHELD_NOTICE

WORKER_ID = "EMP-12345"

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


def _config() -> dict:
    with open(_CONFIG_PATH, encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    return loaded if isinstance(loaded, dict) else {}


class _LeakyTransport:
    """An LLM service whose narrative echoes a worker identifier."""

    def complete(self, messages):
        return {
            "content": (
                "## Summary\nShift closed.\n\n"
                f"## Anomalies\nStoppage acknowledged by {WORKER_ID}.\n\n"
                "## Action Items\n- Review the stoppage."
            )
        }


class _CleanTransport:
    """An LLM service returning an ordinary narrative."""

    def complete(self, messages):
        return {
            "content": (
                "## Summary\nShift closed with one stoppage.\n\n"
                "## Anomalies\nOne stoppage on machine M-101.\n\n"
                "## Action Items\n- Review the stoppage on M-101."
            )
        }


def _payload() -> str:
    return json.dumps(
        {
            "shift_id": "SH-2026-08-31-A",
            "shift_start_time": "2026-08-31T06:00:00Z",
            "log_data": [
                {
                    "timestamp": "2026-08-31T07:00:00Z",
                    "event_type": "STOP",
                    "machine_id": "M-101",
                    "duration_minutes": 120,
                    "alarm_code": "ALM002",
                }
            ],
        }
    )


def _run(llm) -> dict:
    agent = MfgC2004Agent(config=_config(), llm=llm)
    agent.compile()
    return agent.invoke(_payload(), ctx=InvocationContext(caller_trust_level=TrustLevel.INTERNAL))


@pytest.fixture(scope="module")
def withheld() -> dict:
    return _run(_LeakyTransport())


@pytest.fixture(scope="module")
def released() -> dict:
    return _run(_CleanTransport())


class TestViolationIsContained:
    """A refused report must not reach the caller by any route."""

    def test_status_is_error(self, withheld):
        assert withheld.get("status") == "error"

    def test_worker_identifier_absent_from_the_whole_envelope(self, withheld):
        # The whole envelope, not just `output` — a leak through any key is a leak.
        assert WORKER_ID not in json.dumps(withheld, default=str)

    def test_output_is_the_withheld_notice(self, withheld):
        assert withheld.get("output") == WITHHELD_NOTICE

    def test_notice_is_truthy(self, withheld):
        # A falsy notice would fall through to `result` and ship the report.
        assert bool(withheld.get("output"))

    def test_no_report_body_survives(self, withheld):
        envelope = json.dumps(withheld, default=str)
        assert "Manufacturing Shift Report" not in envelope
        assert "Production Summary" not in envelope

    def test_envelope_carries_no_traceback_or_source_path(self, withheld):
        envelope = json.dumps(withheld, default=str)
        assert "Traceback" not in envelope
        assert ".py" not in envelope
        assert "/src/" not in envelope

    def test_refusal_names_the_check_not_the_value(self, withheld):
        for entry in withheld.get("error_log") or []:
            assert WORKER_ID not in entry

    def test_block_happened_at_the_output_boundary(self, withheld):
        # Proves the refusal came from the boundary node rather than an
        # upstream failure that never produced a report at all.
        assert "PostProcessNode" in (withheld.get("node_history") or [])


class TestCleanPathControl:
    """A refuse-everything gate must not be able to pass these tests."""

    def test_clean_request_still_produces_a_real_report(self, released):
        assert released.get("status") == "success"
        output = str(released.get("output"))
        assert "Manufacturing Shift Report" in output
        assert "SH-2026-08-31-A" in output

    def test_clean_report_carries_the_computed_metrics(self, released):
        output = str(released.get("output"))
        assert "Availability" in output
        assert "75.0%" in output

    def test_clean_report_is_not_the_withheld_notice(self, released):
        assert released.get("output") != WITHHELD_NOTICE


class TestOutputBearingFieldsAreDeclared:
    """The cleared-field inventory must keep pace with the state schema.

    A new field carrying report text that is not in the inventory would survive
    a refusal silently, which is the failure mode this guard exists to catch.
    """

    def test_inventory_covers_every_report_bearing_state_field(self):
        from src.nodes.post_process_node import _OUTPUT_BEARING_FIELDS

        assert set(_OUTPUT_BEARING_FIELDS) == {"result", "shift_report", "summary_text"}


class TestMergeOutputContractIsLive:
    """The boundary must read a key something actually writes at its level.

    A layer reading an inner-graph key from outer state compares against an
    absent value on every real invocation: it looks healthy, its tests pass, and
    it never fires. These assertions drive the real pipeline rather than
    constructing state by hand, so they fail if the mapping is ever broken.
    """

    def test_merge_output_populates_the_key_post_process_reads(self, released):
        # PostProcessNode reads state["result"]; merge_output is what writes it.
        assert released.get("output")

    def test_shift_identifier_round_trips_from_caller_to_report(self, released):
        # The renderer reads shift_id from the inner graph's own state. If it
        # read the outer-layer key instead, the header would say UNKNOWN.
        assert "SH-2026-08-31-A" in str(released.get("output"))
        assert "UNKNOWN" not in str(released.get("output"))


class TestClearingIsIndividuallyFalsifiable:
    """The clearing needs its own assertions to be provable.

    Driving the pipeline cannot falsify the clearing on its own: the truthy
    withheld notice already contains the leak, so removing the clearing leaves
    every full-invoke assertion green. Asserting the returned delta directly is
    what makes the second layer testable rather than decorative.

    The clearing still earns its place — the framework's output resolution
    reads `result` whenever `formatted_output` is falsy, and a future override
    surfacing `result` would reintroduce the leak — but it is proved here, not
    by the end-to-end test.
    """

    def _withheld_delta(self) -> dict:
        from src.nodes.post_process_node import PostProcessNode

        node = PostProcessNode()
        state = {
            "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
            "result": f"# Manufacturing Shift Report\nAcknowledged by {WORKER_ID}.",
            "shift_report": f"# Manufacturing Shift Report\nAcknowledged by {WORKER_ID}.",
            "summary_text": f"Acknowledged by {WORKER_ID}.",
        }
        return node.execute(state)

    def test_every_output_bearing_field_is_cleared(self):
        from src.nodes.post_process_node import _OUTPUT_BEARING_FIELDS

        delta = self._withheld_delta()
        for field in _OUTPUT_BEARING_FIELDS:
            assert delta.get(field) == "", f"{field} was not cleared on refusal"

    def test_delta_carries_no_report_text(self):
        assert WORKER_ID not in json.dumps(self._withheld_delta(), default=str)

    def test_delta_reports_error_status(self):
        assert self._withheld_delta()["status"] == "error"
