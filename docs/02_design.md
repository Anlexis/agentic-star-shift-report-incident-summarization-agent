# Design Specification — MFG-C2-004 Manufacturing Shift Report & Incident Summarization Agent

## 1. Position in AgentCore Architecture

- **Template ID**: MFG-C2-004
- **Template Name**: Manufacturing Shift Report & Incident Summarization Agent
- **Category**: Cat 2
- **L1 Base**: `AgentBaseGraph` (outer graph); `BaseGraph` (inner domain workflow graph)
- **Architecture pattern**: Cat 2 nested graph — outer `AgentBaseGraph` backbone wraps an inner `BaseGraph` domain workflow via a `GraphNode` subclass in the `main` slot

### Three-Layer Separation

| Layer | Principle | Implementation |
|-------|-----------|----------------|
| State | Composition (flat TypedDict) | Primitives + JSON-serializable types only; no Pydantic, no credentials |
| Node | Inheritance (Template Method) | `FunctionNode` subclasses; override `execute(self, state) -> dict` only. Runtime configuration is injected at construction — the framework calls `execute(state)` with no config argument |
| Graph | Composition (Builder) | Outer: `register_nodes()` only; Inner: `register_nodes()` + `add_edges()` |

---

## 2. Architecture Overview

MFG-C2-004 uses the **Cat 2 two-layer nested architecture** mandated for all multi-step domain
templates. Domain complexity is fully encapsulated in the inner graph; the outer backbone
remains identical to Cat 1 and is never modified.

### 2-1. Two-Layer Layout

```
src/
├── graph/
│   ├── graph.py                   ← Outer graph (AgentBaseGraph; L1 Base)
│   └── domain_workflow_graph.py   ← Inner graph (BaseGraph; custom topology)
├── nodes/
│   ├── pre_process_node.py        ← Input screening + worker-ID redaction
│   ├── post_process_node.py       ← Output boundary
│   ├── log_ingestion_node.py      ← Domain node 1
│   ├── data_normalization_node.py ← Domain node 2
│   ├── anomaly_detection_node.py  ← Domain node 3
│   ├── summarization_node.py      ← Domain node 4
│   └── shift_report_render_node.py← Domain node 5
└── schemas/
    └── state.py                   ← Flat TypedDict
```

### 2-2. Outer Graph Pipeline (fixed backbone — do NOT override `add_edges()`)

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                       ↓ (RETRY, max 3)
                                    pre_process
```

- `initialize` / `finalize`: framework defaults via `super().register_nodes()`
- `pre_process`: `PreProcessNode` — size cap, injection screen, worker-identifier redaction
- `main`: `ShiftReportGraphNode(GraphNode)` — delegates to `DomainWorkflowGraph`
- `post_process`: `PostProcessNode` — output boundary; refuses and clears the report on a credential or worker-identifier finding

### 2-3. Inner Domain Workflow Pipeline (custom topology)

```
START → log_ingestion → data_normalization → anomaly_detection → summarization → shift_report_render → END
```

All inner nodes are `FunctionNode` subclasses. The inner graph is `BaseGraph` (fully custom
topology — no forced backbone, no initialize/finalize).

---

## 3. Outer Graph — `src/graph/graph.py`

### Class: `MfgC2004Agent(AgentBaseGraph)`

Inherits the **L1 Base** (`AgentBaseGraph`). Implements only `register_nodes()`.
`add_edges()` is **not overridden** — backbone wiring belongs to the framework.

```python
from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.post_process_node import PostProcessNode
from src.schemas.state import State


class ShiftReportGraphNode(GraphNode):
    """GraphNode subclass assigned to the `main` slot.
    Wraps DomainWorkflowGraph (inner Cat 2 graph).
    """
    error_strategy: ClassVar[str] = "propagate"

    def get_subgraph(self):
        from src.graph.domain_workflow_graph import DomainWorkflowGraph
        return DomainWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        # Prefer validated_input (set by pre_process after PII strip)
        return state.get("validated_input", state.get("user_input", ""))

    def merge_output(self, state: AgentState, sub_result: dict) -> dict:
        # Return ONLY changed keys — never the full state
        return {
            "shift_report": sub_result.get("shift_report"),
            "status":       sub_result.get("status"),
        }

    def _parent_config(self) -> dict:
        return {}


class MfgC2004Agent(AgentBaseGraph):
    """Outer graph — L1 Base (AgentBaseGraph).

    Domain logic is encapsulated in ShiftReportGraphNode (main slot).
    add_edges() is NOT overridden.
    """

    @property
    def name(self) -> str:
        return "mfg_c2_004"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()                              # initialize + finalize
        self._nodes["pre_process"]  = PreProcessNode()
        self._nodes["main"]         = ShiftReportGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden — backbone belongs to the framework
```

**Rules enforced:**
- `super().register_nodes()` called first (fills initialize + finalize)
- `ShiftReportGraphNode` assigned to `self._nodes["main"]`
- `add_edges()` not overridden on the outer graph
- `merge_output()` returns only changed keys

---

## 4. GraphNode Contract — `ShiftReportGraphNode`

| Method | Responsibility |
|--------|---------------|
| `get_subgraph()` | Instantiate and return `DomainWorkflowGraph`; called per `execute()` |
| `extract_input(state)` | Return `state["validated_input"]` (set by `pre_process` after PII strip); fallback to `user_input` |
| `merge_output(state, sub_result)` | Map inner result (`shift_report`, `status`) into outer state delta; return **only changed keys** |
| `error_strategy` | `"propagate"` — re-raise inner errors as `SubgraphError` (fail-fast) |

`sub_result` keys consumed by `merge_output()` are defined in `DomainWorkflowGraph.get_output()` — both are designed together to ensure field name consistency.

---

## 5. Inner Graph — `src/graph/domain_workflow_graph.py`

### Class: `DomainWorkflowGraph(BaseGraph)`

Inherits `BaseGraph` (fully custom topology). Implements all **7 BaseGraph ABC methods**.
`register_nodes()` does **not** call `super()` (abstract in `BaseGraph`).
Does **not** register `initialize` / `finalize` — those are outer backbone concerns.

#### 5-1. The 7 ABC Methods

| Method | Implementation notes |
|--------|---------------------|
| `name` | `"mfg_c2_004_domain_workflow"` |
| `state_schema` | returns `State` |
| `_validate_config` | validates `system_prompt`, `report_template_path`, `shift_duration_hours`; raises `ConfigError` if absent |
| `register_nodes` | assigns the 5 domain nodes; no `super()` call; no initialize/finalize |
| `add_edges` | linear: `log_ingestion → data_normalization → anomaly_detection → summarization → shift_report_render → END` |
| `route` | required by ABC; for linear topology returns `END` on error, `"shift_report_render"` otherwise; only called if `add_conditional_edges()` references it |
| `get_output` | shapes `sub_result` dict consumed by `ShiftReportGraphNode.merge_output()` |

#### 5-2. Node Registration

```python
def register_nodes(self) -> None:
    self._nodes["log_ingestion"]       = LogIngestionNode()
    self._nodes["data_normalization"]  = DataNormalizationNode()
    self._nodes["anomaly_detection"]   = AnomalyDetectionNode()
    self._nodes["summarization"]       = SummarizationNode()
    self._nodes["shift_report_render"] = ShiftReportRenderNode()
```

#### 5-3. Edge Wiring

```python
def add_edges(self) -> None:
    self._sg.add_edge(START, "log_ingestion")
    self._sg.add_edge("log_ingestion",      "data_normalization")
    self._sg.add_edge("data_normalization",  "anomaly_detection")
    self._sg.add_edge("anomaly_detection",   "summarization")
    self._sg.add_edge("summarization",       "shift_report_render")
    self._sg.add_edge("shift_report_render", END)
```

#### 5-4. Output Shape

```python
def get_output(self, state: AgentState) -> dict:
    return {
        "shift_report":       state.get("shift_report"),
        "status":             state.get("status"),
        "anomaly_highlights": state.get("anomaly_highlights", []),
        "metrics":            state.get("metrics", {}),
        "trace_id":           state.get("trace_id"),
        "correlation_id":     state.get("correlation_id"),
        "node_history":       state.get("node_history", []),
    }
```

Fields `shift_report` and `status` are mapped by `ShiftReportGraphNode.merge_output()`.
`anomaly_highlights`, `metrics`, `trace_id`, `correlation_id`, `node_history` are available
in the outer state after `merge_output()` returns (extend the outer merge if needed).

---

## 6. Domain Nodes (`src/nodes/*.py`)

All domain nodes are `FunctionNode` subclasses. Each overrides `execute(self, state, config) -> dict`
returning only the state keys it modifies (partial dict). Domain nodes are registered by the
**inner** graph, not the outer.

### Node Configuration

| Node | File | Responsibility | Input State Keys | Output State Keys |
|------|------|---------------|-----------------|------------------|
| `LogIngestionNode` | `log_ingestion_node.py` | Parse raw shift log payload; validate log schema; reject malformed records | `validated_input` | `raw_log_records`, `log_ingestion_errors` |
| `DataNormalizationNode` | `data_normalization_node.py` | Normalize timestamps, units, event codes to canonical format | `raw_log_records` | `normalized_records` |
| `AnomalyDetectionNode` | `anomaly_detection_node.py` | Detect stoppages, out-of-threshold events, incident flags; compute OEE metrics | `normalized_records` | `anomaly_highlights`, `metrics` |
| `SummarizationNode` | `summarization_node.py` | LLM call: summarize anomalies + production data → structured narrative using `system_prompt` | `normalized_records`, `anomaly_highlights`, `metrics` | `summary_text` |
| `ShiftReportRenderNode` | `shift_report_render_node.py` | Render final structured report using `report_template_path`; populate action items | `summary_text`, `anomaly_highlights`, `metrics` | `shift_report`, `status` |

### Node Import Pattern

Each node imports from `framework.*` only:

```python
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
```

No `agenticstar` imports. No sibling template imports.

### `required_trust_level`

All domain nodes declare:

```python
required_trust_level: ClassVar[TrustLevel] = TrustLevel.INTERNAL
```

Manufacturing shift logs are internal operational data — `INTERNAL` trust level enforced.

---

## 7. State Schema — `src/schemas/state.py`

Flat `TypedDict` covering both outer and inner layers. No Pydantic, no dataclasses,
no credentials, no `InvocationContext`.

```python
from typing import TypedDict, Optional, List, Dict, Any
from framework.schemas.agent_state import AgentState


class State(AgentState):
    # Outer layer fields (set by pre_process / merge_output)
    user_input:         str
    validated_input:    str           # PII-stripped input set by PreProcessNode
    shift_report:       Optional[str] # Final rendered shift report
    status:             Optional[str] # AgentStatus value

    # Inner layer fields (set by domain nodes)
    raw_log_records:    Optional[List[Dict[str, Any]]]  # Parsed log entries
    log_ingestion_errors: Optional[List[str]]           # Malformed record notes
    normalized_records: Optional[List[Dict[str, Any]]]  # Canonical-form records
    anomaly_highlights: Optional[List[Dict[str, Any]]]  # Detected anomalies/stoppages
    metrics:            Optional[Dict[str, Any]]        # OEE and production metrics
    summary_text:       Optional[str]                   # LLM-generated narrative

    # Tracing / audit (framework-managed)
    trace_id:           Optional[str]
    correlation_id:     Optional[str]
    node_history:       Optional[List[str]]
```

**State Constraints (mandatory):**
- Flat TypedDict only — primitives + JSON-serializable types
- No JWT, API keys, credentials in State (checkpoint DB leakage risk)
- `InvocationContext` via `config["configurable"]` only — not in State
- No Pydantic models, dataclasses, arbitrary Python objects (msgpack incompatible)
- Worker IDs are PII — stripped by `PreProcessNode` before State is populated

---

## 8. Config Surface — `config/agent.yaml`

```yaml
agent_id: mfg_c2_004
template_id: MFG-C2-004
category: Cat 2
industry: MFG

llm:
  model: gpt-4o
  temperature: 0.2
  max_tokens: 2048

security:
  s3_gate_enabled: true
  required_trust_level: INTERNAL

mfg_c2_004:
  system_prompt: |
    You are a manufacturing shift report assistant. Summarize the following
    shift log data, highlighting anomalies, stoppages, and action items.
    Use structured output with sections: Summary, Anomalies, Production Metrics,
    Action Items.
  report_template_path: config/shift_report_template.md
  shift_duration_hours: 8
  metrics_to_compute:
    - oee
    - availability
    - performance_rate
    - quality_rate
  anomaly_highlight_threshold: 0.15   # Events exceeding 15% of shift time flagged
  max_log_records_per_shift: 5000
```

Runtime values live in `config/config.yaml`; `config/agent.yaml` is the static manifest and
carries no runtime block. The runtime mapping is handed to `MfgC2004Agent(config=...)`, forwarded
by `ShiftReportGraphNode._parent_config()` to `DomainWorkflowGraph`, and injected into each domain
node's constructor. Construction is the only channel available: the framework invokes a node as
`node(state)` and calls `execute(state)` with no config argument, so a node that reads settings
from a per-call argument reads nothing and silently falls back to its built-in defaults.

---

## 9. Security Model

### Input boundary (`pre_process`)

`PreProcessNode` runs before State is populated with caller-provided data:
- size cap on the serialized payload — oversized input is refused, never truncated;
- injection screen covering chat-template control tokens (`<|...|>`, `[INST]`, `<<SYS>>`) as a
  class, plus instruction-override phrases. The text is screened twice, raw and with markup
  stripped, because stripping markup can turn a detectable control token into undetectable plain
  text while re-assembling a directive spliced across tags;
- worker-identifier redaction before the payload is written to `validated_input`, so personal data
  never reaches State even if the payload later fails to parse.

Refusal happens in the node that owns the caller contract, not only in a framework gate that a
deployment may configure off. Errors name the offending field and never echo the rejected value.

### Ingestion validation (`log_ingestion`)

Every caller field is validated against an explicit bound before it is stored:
- numerics pass through a finite + bounded parser. NaN and the infinities parse successfully via
  `float()` and then compare False against every threshold, so an unchecked non-finite value
  silently disables the comparison this agent exists to make;
- identifiers echoed into the report (`machine_id`, `line_id`, `product_id`, `shift_id`,
  `alarm_code`) are restricted to the inert render alphabet `[A-Za-z0-9_-]{1,32}`. A
  non-conforming identifier is refused rather than rewritten: silently altering a machine or part
  identifier changes what the report says about a physical asset;
- event types and severities come from closed vocabularies; an unrecognised value becomes
  `UNKNOWN` rather than being echoed;
- free-text notes are screened and length-capped;
- the configured record cap is honoured but can never exceed the module ceiling.

### Output boundary (`post_process`)

`PostProcessNode` runs two independent checks, each with its own audit event:
- a credential scan using the framework's own `detect_credentials_in_value`. Using the same
  function the framework's gate calls keeps the two block sets identical — a value this node
  passed and the framework caught would make the framework raise after the node returned, and the
  wrapper discards the node's whole delta on an exception, including its clearing;
- a worker-identifier scan, which the framework does not perform.

On a finding the node returns ERROR **and clears every output-bearing field**
(`result`, `shift_report`, `summary_text`), replacing the output with a truthy withheld notice.
Clearing is the containment step rather than the error status: the framework resolves an agent's
output as `state.get("formatted_output") or state.get("result")` with no status check, so
returning ERROR while leaving `result` populated would still ship the ungated report inside the
error envelope — and a falsy notice would re-activate the same fallback.

### Access control

Shift reports carry plant operational data. Every node declares
`required_trust_level`, and the domain nodes require `TrustLevel.INTERNAL`, so the agent is not
reachable from a lower-trust caller at all. Report access scoping — which shifts a given caller
may query — belongs to the caller-facing gateway, not to agent state; see the operation guide.

### Audit trail

The framework emits `node_start` / `node_complete` / `node_error` around every node. In addition,
every boundary node in this template emits a domain event on its execution path
(`shift_input_accepted`, `shift_log_ingested`, `shift_metrics_computed`,
`shift_anomalies_detected`, `shift_narrative_generated`, `shift_report_rendered`,
`shift_report_released` / `shift_report_withheld`), each carrying `trace_id` and
`correlation_id`. Refusal events name the check that fired, never the value that triggered it.

### Report precision

This agent reports operational quantities — rates, unit counts, durations. It renders **no
monetary values**, so the rounding grid applied by report-generating agents that do (monetary
aggregates snapped to the nearest 1,000) does not apply here and is deliberately absent. That
absence is pinned by tests rather than assumed: such a grid reads any standalone three-letter
uppercase token as a currency marker, which would corrupt exactly the values this agent exists to
report — a machine identifier such as `SKF-6205` would render as `SKF-6,000`. The invariant this
report does carry is fidelity: every number rendered is the number the pipeline computed, and
every identifier rendered is the identifier the caller supplied.

---

## 10. Data / KB Requirements

- **No external vector store or retrieval API** — this is a DocGen/Summarization pattern, not RAG
- **Input**: shift log payload passed directly as `user_input` (JSON-serializable dict or string)
- **LLM**: standard framework LLM client configured via `config/agent.yaml`
- **Template file**: `config/shift_report_template.md` — Jinja2 or equivalent template for rendering the final report structure; committed to the repo

---

## 11. Framework Components Used

| Component | Usage |
|-----------|-------|
| `AgentBaseGraph` | Outer graph base (L1 Base) |
| `BaseGraph` | Inner graph base (custom topology) |
| `GraphNode` | `ShiftReportGraphNode` — wraps inner graph in `main` slot |
| `FunctionNode` | All 5 domain nodes |
| `AgentState` | State base (extended by `State` TypedDict) |
| `InvocationContext` | Passed via `config["configurable"]`; NOT stored in State |
| `_extra_security_gate_input()` | Domain input-gate extension hook |
| `_extra_security_gate_output()` | Domain output-gate extension hook |
| `emit_trace_event()` | Domain audit events, emitted from `execute()` |
| `SecurityViolationError` | Raised on gate violations |
| `TrustLevel.INTERNAL` | `required_trust_level` for all nodes |

**Import isolation:**
- No direct platform-SDK imports
- All imports from `framework.*` or `src.*` only

---

## 12. Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | `AgentBaseGraph` | `AutonomousBaseGraph` | `AgentBaseGraph` | Fixed pipeline — no autonomous think/act loop; Cat 2 multi-step pipeline |
| Inner graph topology | `BaseGraph` (custom) | `AgentBaseGraph` (backbone) | `BaseGraph` | Domain nodes use fully custom names (log_ingestion, anomaly_detection, etc.); no pre/main/post slots needed in the inner layer |
| Composition pattern | `GraphNode` (subgraph) | `RemoteAgentNode` (HTTP) | `GraphNode` | Inner workflow is local; no remote call needed; error propagation via `SubgraphError` |
| Error strategy | `"propagate"` | `"handle"` | `"propagate"` | Shift report generation should fail fast; no graceful degradation on inner errors |
| Trust level | `VERIFIED_EXTERNAL` | `INTERNAL` | `INTERNAL` | Shift logs are internal MES data; external callers not expected |
| PII handling | Strip in pre_process | Strip in each node | Strip in `pre_process` | Centralized PII removal before State population; nodes never see raw worker IDs |

---

## 13. Diagram — Full Data Flow

```
Caller (InvocationContext, TrustLevel.INTERNAL)
  │
  ▼
[Outer: MfgC2004Agent (AgentBaseGraph — L1 Base)]
  │
  ├─ initialize          (framework default)
  │
  ├─ pre_process         PreProcessNode
  │   ├── Size cap + injection screen (raw and markup-stripped)
  │   ├── Worker ID PII strip → state["validated_input"]
  │   └── Raw input NOT persisted beyond this node
  │
  ├─ main                ShiftReportGraphNode (GraphNode)
  │   ├── extract_input()  → state["validated_input"]
  │   ├── get_subgraph()   → DomainWorkflowGraph
  │   │
  │   │  [Inner: DomainWorkflowGraph (BaseGraph)]
  │   │    ├─ log_ingestion        parse + validate log schema → raw_log_records
  │   │    ├─ data_normalization   canonical timestamps/units  → normalized_records
  │   │    ├─ anomaly_detection    stoppages / OEE metrics     → anomaly_highlights, metrics
  │   │    ├─ summarization        LLM narrative               → summary_text
  │   │    └─ shift_report_render  template render             → shift_report, status
  │   │
  │   └── merge_output() → { shift_report, status }
  │
  ├─ post_process        PostProcessNode
  │   ├── Credential scan (the framework's own detector)
  │   ├── Worker-identifier scan
  │   └── On a finding: ERROR + every output-bearing field cleared
  │
  └─ finalize            (framework default)

Output: structured shift_report + status
```

---

## 14. Reference Pattern Files

- Outer graph: the nested Cat 2 reference layout — `AgentBaseGraph` with a `GraphNode` in the `main` slot
- Inner graph: a `BaseGraph` subclass owning its own topology
