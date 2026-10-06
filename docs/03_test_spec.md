# Test Specification — MFG-C2-004 Manufacturing Shift Report & Incident Summarization Agent

**Template ID**: MFG-C2-004  
**Coverage target**: all 5 domain nodes + outer graph backbone (security gates)  
**Test types**: Unit / Proof-of-Boundary

---

## 1. Test Strategy

MFG-C2-004 is a Cat 2 two-layer nested graph:

- **Outer layer** (`AgentBaseGraph`): `initialize → pre_process → main → post_process → finalize`
- **Inner layer** (`BaseGraph` / `DomainWorkflowGraph`): `log_ingestion → data_normalization → anomaly_detection → summarization → shift_report_render`

The test strategy targets:

1. **Framework compliance** — verify the 5-layer security model, import isolation, and state safety using unit-level assertions and AST scanning.
2. **Domain-node business logic** — verify each domain node's output against known inputs (TC-01–TC-06 below).
3. **Proof-of-Boundary** — verify the security boundary between raw input and sanitized state/output (PB-01–PB-03).

Unit and boundary tests exercise the domain nodes directly; the integration suite drives the real ASGI `/invoke` entry point end to end. `LogIngestionNode` and `DataNormalizationNode` are deterministic; `SummarizationNode` is driven with a stub LLM service and is verified for raw-log-retention compliance.

---

## 2. Framework Compliance Tests (Mandatory — TC-01–TC-08)

These map to the mandatory proof-of-boundary matrix for this template family.

| TC-ID | Description | Method | Expected Result |
|-------|-------------|--------|----------------|
| TC-01 | State contract: `State` is a flat `TypedDict` subclass with no `BaseModel`/dataclass fields | AST scan of `src/schemas/state.py` | All annotated fields use primitives or standard-library generics only; 0 `BaseModel` / `dataclass` annotations |
| TC-02 | `SecurityViolationError` fires on invalid input | Pass empty/missing `user_input` to `PreProcessNode.execute()` | `status == AgentStatus.ERROR`; `error_log` populated; no exception escapes |
| TC-03 | No JWT or credential-like fields in `State` | AST scan of `src/schemas/state.py` for `jwt`, `token`, `api_key`, `secret`, `password`, `credential`, `connection_string` field names | 0 violations |
| TC-04 | `InvocationContext` accessed via `config["configurable"]` only | AST scan of all `src/**/*.py` for `state["invocation_context"]` or `state.get("invocation_context")` | 0 violations |
| TC-05 | Audit events fire on node execution | The framework emits `node_start` / `node_complete` / `node_error` around every node; in addition every boundary node emits a domain event on a reachable path in `execute()` | Framework events unsuppressed; every `FunctionNode.execute()` in `src/` emits a domain `emit_trace_event(...)` |
| TC-06 | `_security_gate_input()` is non-bypassable | AST scan of `src/**/*.py` — no node overrides `_security_gate_input()` to return early or empty | 0 bypass overrides in `src/` |
| TC-07 | `_security_gate_output()` is non-bypassable | AST scan of `src/**/*.py` — no node overrides `_security_gate_output()` to return early or empty | 0 bypass overrides in `src/` |
| TC-08 | `required_trust_level` declared on all domain nodes | Check each `FunctionNode` subclass in `src/nodes/` declares `required_trust_level: ClassVar[TrustLevel]` | All 5 domain nodes + `PreProcessNode` + `PostProcessNode` declare `required_trust_level`; value is `TrustLevel.INTERNAL` |

---

## 3. Domain Business Logic Tests (TC-BL-01–TC-BL-06)

These tests invoke domain nodes directly with synthetic MES log fixtures.  
`SummarizationNode` is exercised with no LLM service provisioned; it composes the narrative from the computed metrics and anomalies, which is the same path a deployment without a model service takes.

### TC-BL-01 — Normal Shift: Valid MES Log → Report with Correct Metrics

**Purpose**: Happy-path — a well-formed 8-hour shift log produces a complete shift report with OEE metrics.

**Input** (passed as `validated_input`):
```json
{
  "shift_id": "SHIFT-001",
  "shift_start_time": "2026-06-17T06:00:00Z",
  "log_data": [
    {"timestamp": "2026-06-17T06:00:00Z", "event_type": "PRODUCTION",
     "machine_id": "MC-01", "units_produced": 240, "units_target": 250,
     "units_rejected": 5},
    {"timestamp": "2026-06-17T07:00:00Z", "event_type": "ALARM",
     "machine_id": "MC-01", "alarm_code": "ALM004", "duration_minutes": 10}
  ]
}
```

**Steps**:
1. `LogIngestionNode.execute()` → assert `raw_log_records` has 2 entries; `log_ingestion_errors == []`
2. `DataNormalizationNode.execute()` on the output state → assert `normalized_records` has 2 entries; `metrics["oee"]` is a float in `(0.0, 1.0]`; `metrics["downtime_minutes"] == 10.0`
3. `AnomalyDetectionNode.execute()` → assert `anomaly_highlights` is a list (may be empty for short downtime)
4. `ShiftReportRenderNode.execute()` (with stub `summary_text`) → assert `shift_report` is a non-empty string containing `"SHIFT-001"`; `status == AgentStatus.SUCCESS`

**Expected**: Pipeline completes; `shift_report` contains shift ID and OEE metrics table.

---

### TC-BL-02 — Anomaly Burst: Downtime Exceeds Threshold → Anomaly Highlighted

**Purpose**: Verify `AnomalyDetectionNode` flags a stoppage that exceeds `anomaly_highlight_threshold` (15% of shift).

**Input** — normalized records include a STOP event of 90 minutes (90/480 = 18.75% > 15%):
```python
normalized_records = [
    {"timestamp": "2026-06-17T06:00:00Z", "event_type": "STOP",
     "machine_id": "MC-02", "duration_minutes": 90.0,
     "alarm_code": "STOP002", "alarm_description": "Unplanned downtime"}
]
metrics = {"oee": 0.75, "reject_rate": 0.01}
```

**Expected**: `anomaly_highlights` contains at least one entry with `anomaly_type == "stoppage"` and `machine_id == "MC-02"`; `severity` in `{"MEDIUM", "HIGH"}`.

---

### TC-BL-03 — Partial Input: Missing Supervisor Notes → Report Still Generated

**Purpose**: Verify the pipeline tolerates missing optional fields (`notes`, supervisor commentary) without failing.

**Input**: Valid shift log payload with `log_data` records that omit the optional `notes` key.

**Expected**:
- `LogIngestionNode.execute()` succeeds; `raw_log_records` populated
- Pipeline reaches `ShiftReportRenderNode`
- `shift_report` is non-empty; `status == AgentStatus.SUCCESS`
- No unhandled `KeyError` or `AttributeError` raised

---

### TC-BL-04 — High Anomaly Count: Top-N Anomaly Highlighting

**Purpose**: Verify the pipeline handles a large number of anomaly events without crashing; all detected anomalies appear in `anomaly_highlights`.

**Input** — normalized records contain 10 STOP events each lasting 30 minutes (30/480 = 6.25%, below 15% individual threshold), plus 6 ALARM events from the same machine within a 30-minute window (triggers alarm burst detection).

**Expected**:
- `AnomalyDetectionNode.execute()` returns `anomaly_highlights` with at least one `anomaly_type == "alarm_burst"` entry
- `ShiftReportRenderNode` renders the report without truncation errors; all anomaly entries appear in the rendered string
- No crash on large `anomaly_highlights` list

---

### TC-BL-05 — OEE Edge Case: Zero Production Units Handled Gracefully

**Purpose**: Verify `DataNormalizationNode` handles zero `units_produced` without division-by-zero.

**Input**: shift log with `units_produced = 0`, `units_target = 250`, `units_rejected = 0`.

**Expected**:
- `metrics["oee"]` is `0.0` (availability × performance × quality with performance = 0)
- `metrics["quality_rate"]` is `1.0` (no units to reject → quality defaults to 1.0)
- `metrics["reject_rate"]` is `0.0`
- No `ZeroDivisionError` raised
- `AnomalyDetectionNode` flags `anomaly_type == "low_oee"` (OEE = 0.0 < 0.65 threshold)

---

### TC-BL-06 — Long Incident: Multi-Line Incident Description Truncated/Summarized

**Purpose**: Verify that a very long `notes` or alarm description in a log record is capped before being written to State and does not cause template rendering errors.

**Input**: A log record where the `notes` field contains a 10,000-character string.

**Expected**:
- `DataNormalizationNode` caps the `notes` field at 500 characters (see `norm[extra_key] = val[:500]` in implementation)
- `ShiftReportRenderNode` renders the report without memory or template errors
- The rendered `shift_report` does not contain the full 10,000-character string

---

## 4. Proof-of-Boundary Tests (PB-01–PB-03)

These tests verify the security boundary between raw input data and the sanitized State/output. They import and exercise the actual node implementations.  
Location: `tests/proof_of_boundary/`

### PB-01 — Worker ID PII Gate: Worker ID Stripped from State and Output

**Boundary**: `PreProcessNode` + `LogIngestionNode` (worker-identifier redaction)

**Security requirement**: Worker IDs and employee numbers are PII. They must not appear in `State` fields or the final `shift_report` after `LogIngestionNode` processes the payload.

**Test**:
1. Craft an input payload whose `log_data` records contain worker identifiers in known PII-bearing field names (`worker_id`, `employee_id`) and in string values matching PII regex patterns (`EMP-12345`, `WORKER_99`).
2. Invoke `LogIngestionNode.execute()` with this state.
3. Serialize `raw_log_records` from the output to a string.
4. Assert that no PII token (`EMP-12345`, `WORKER_99`, the raw worker ID value) appears in the serialized output.
5. Also run `ShiftReportRenderNode` on the pipeline output and assert the final `shift_report` does not contain the original PII values.

**Expected**: All PII fields are replaced with `"[REDACTED]"`; the final report contains no raw worker identifiers.

**Implementation file**: `tests/proof_of_boundary/test_pii_strip.py`

---

### PB-02 — Raw Log Retention Gate: Raw Log Not in State/Output After Processing

**Boundary**: `SummarizationNode` + `ShiftReportRenderNode` (raw log retention)

**Security requirement**: Raw MES log records must not be retained in the final State or agent output. `summary_text` and `shift_report` should contain only aggregated/summarized information, not the verbatim per-record log data.

**Test**:
1. Craft an input payload with log records containing a distinctive marker string (e.g., `"RAW_LOG_SENTINEL_XYZ"`) in a non-PII field such as `notes`.
2. Run the pipeline through `LogIngestionNode` → `DataNormalizationNode` → `SummarizationNode` (stub LLM) → `ShiftReportRenderNode`.
3. Assert that the final state **does not** contain `raw_log_records` as a direct field in the output returned by `ShiftReportRenderNode` (the partial dict).
4. Assert that the final `shift_report` string does not contain the raw sentinel string verbatim (only aggregated data reaches the narrative — `_build_record_digest` emits event-type counts only).
5. Assert that `SummarizationNode`'s `summary_text` output does not contain the sentinel string as a raw record (sentinel may appear in the truncated sample block, but raw per-record blobs are not the `summary_text` output field itself).

**Expected**: `ShiftReportRenderNode` partial dict does not include `raw_log_records`; the rendered `shift_report` contains no verbatim raw record dump.

**Implementation file**: `tests/proof_of_boundary/test_pii_strip.py` (combined with PB-01)

---

### PB-03 — Malformed Log Schema: Ingestion Rejects Invalid Input

**Boundary**: `LogIngestionNode` (payload schema validation)

**Security requirement**: Structurally invalid MES log payloads must be rejected at `LogIngestionNode` with a descriptive error. No partial processing should occur; `raw_log_records` must be empty on rejection.

**Test cases**:

| Sub-case | Input | Expected |
|----------|-------|---------|
| PB-03a | Payload is a plain string (not JSON object) | `raw_log_records == []`; `log_ingestion_errors` contains `"Input validation failed"` |
| PB-03b | Payload missing `shift_id` (required field) | `raw_log_records == []`; `log_ingestion_errors` contains `"missing required fields"` |
| PB-03c | Payload missing `log_data` (required field) | `raw_log_records == []`; `log_ingestion_errors` contains `"missing required fields"` |
| PB-03d | `log_data` is not a list (e.g., a bare string) | `raw_log_records == []`; `log_ingestion_errors` contains `"log_data must be a list"` |
| PB-03e | Individual log entry missing `event_type` (required per-record field) | That entry is skipped; `log_ingestion_errors` contains a per-record skip message |

**Expected**: For all sub-cases, no unhandled exception is raised; `raw_log_records` is always an empty list on hard schema failures; `log_ingestion_errors` provides a clear rejection message naming the field.

**Implementation file**: `tests/proof_of_boundary/test_malformed_log.py`

---

## 5. Migration Suites

The suites below were added when the template moved to the flat-manifest runtime contract. They
cover the properties the earlier suite could not see: it passed with the runtime configuration
entirely inert, because a node reading configuration from a per-call argument reads nothing and
falls back to its defaults.

### PB-04 — Output Boundary and Containment

**Boundary**: `PostProcessNode`

**Requirement**: a report that fails a boundary check is not released by ANY route. The framework
resolves output as `state.get("formatted_output") or state.get("result")` with no status check, so
an error status alone does not withhold anything.

The fault is injected on the DATA path — an LLM service whose narrative echoes a worker identifier
— never by patching the gate. Worker identifiers are the reachable case: the framework's own
output scan already covers credential shapes, but not personal data.

| Case | Expected |
|------|----------|
| PB-04a | Envelope carries no worker identifier, no report body, no traceback, no source path |
| PB-04b | Output is the withheld notice, and the notice is truthy |
| PB-04c | Every output-bearing field (`result`, `shift_report`, `summary_text`) is cleared |
| PB-04d | `PostProcessNode` appears in `node_history`, proving the block happened at the boundary |
| PB-04e | Clean-path control: the same request without the identifier still returns a real report |

**Implementation file**: `tests/proof_of_boundary/test_output_boundary.py`

### PB-05 — Report Invariants

**Boundary**: `ShiftReportRenderNode`

**Requirement**: numbers and identifiers are rendered as computed and as supplied. This agent
renders no monetary values, so no rounding grid applies; the absence is pinned so one cannot be
introduced silently. Bearing codes (`SKF-6205`), shift codes and pure-numeric asset codes must
survive byte-identical, and the rendered report must contain no unexpanded template markup.

**Implementation file**: `tests/proof_of_boundary/test_report_invariants.py`

### Validation contract

Parametrized coverage of every caller-controlled field: a non-finite matrix (`NaN`, `Infinity`,
`-Infinity`, raw floats, over-magnitude) per numeric field; identifier acceptance and rejection
over the real render alphabet; and the injection screen in both directions — chat-template control
tokens and directive phrases refused, real maintenance-log sentences not refused.

**Implementation file**: `tests/unit/test_validation_contract.py`

### Configuration reach-through

Asserts every declared runtime key has a reader in `src/`, and that changing a declared value
changes the report end to end. Reach-through is proven by observing a changed OUTPUT rather than by
inspecting an attribute: a value stored but never consulted would pass an attribute check and still
be dead.

**Implementation file**: `tests/unit/test_config_reach_through.py`

### End-to-end through the ASGI entry point

Drives the real `/invoke` adapter with the Bearer credential that satisfies the trust gate: report
generation from caller data, every anomaly path, validation rejection, the credential screen on
`input_context` (refused 400, naming the field, never echoing the value), the unknown-key drop, and
the size caps.

**Implementation file**: `tests/integration/test_invoke_e2e.py`

---

## 6. Test Execution Summary

| Category | Count |
|----------|-------|
| Framework compliance (TC-01–TC-08) | 8 |
| Business logic (TC-BL-01–TC-BL-06) | 6 |
| Proof-of-Boundary (PB-01–PB-05) | 5 |
| Migration suites (validation, config, end-to-end) | 3 |

**Execution**: `python -m pytest tests/ -v`  
**PB tests specifically**: `python -m pytest tests/proof_of_boundary/ -v`
