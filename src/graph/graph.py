"""AgentCore Platform v1.0"""

# MFG-C2-004 — Outer graph (AgentBaseGraph; Cat 2 two-layer nested architecture)
#
# Architecture (Cat 2):
#
#   Outer backbone (fixed — identical to Cat 1, do NOT override add_edges()):
#     START → initialize → pre_process → main → {route} → post_process → finalize → END
#                                             ↓ (RETRY, max 3)
#                                          pre_process
#
#   `main` slot is a GraphNode subclass (ShiftReportGraphNode) that delegates
#   the full domain workflow to DomainWorkflowGraph (inner BaseGraph).
#
#   Domain complexity is fully encapsulated inside the inner graph. The outer
#   backbone is never modified.
#
# Directory layout:
#   src/graph/graph.py                 ← outer graph (this file)
#   src/graph/domain_workflow_graph.py ← inner graph (multi-step topology)
#
# Rules enforced:
#   ✅ MfgC2004Agent inherits AgentBaseGraph
#   ✅ super().register_nodes() called first (fills initialize + finalize)
#   ✅ ShiftReportGraphNode assigned to self._nodes["main"]
#   ✅ merge_output() returns only changed keys
#   ❌ add_edges() NOT overridden on the outer graph
#   ❌ No direct platform-SDK imports

from typing import Any, ClassVar, Dict, Optional

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State


class ShiftReportGraphNode(GraphNode):
    """GraphNode subclass assigned to the `main` slot of MfgC2004Agent.

    Wraps DomainWorkflowGraph (inner Cat 2 BaseGraph).
    Called by AgentBaseGraph backbone after pre_process and before post_process.

    Contracts:
      get_subgraph()    — instantiate and return DomainWorkflowGraph
      extract_input()   — pull validated_input (PII-stripped) from outer state
      merge_output()    — map sub_result fields into outer state delta (changed keys only)
      error_strategy    — "propagate": re-raise inner errors as SubgraphError (fail-fast)
    """

    # "propagate": re-raise inner graph exceptions as SubgraphError (default — fail fast).
    # "handle": call on_subgraph_error() instead — use for graceful degradation.
    error_strategy: ClassVar[str] = "propagate"

    # False: HITL interrupts are handled inside the inner graph only.
    # True: surface inner HITL interrupt to the outer caller.
    propagate_hitl: ClassVar[bool] = False

    def __init__(self, config: Optional[Dict[str, Any]] = None, llm: Optional[Any] = None) -> None:
        """Capture the outer agent's runtime config and LLM for the inner graph.

        The inner graph and its nodes are constructed here, so the runtime
        values they read must be handed down explicitly at construction time.
        LangGraph invokes a node as ``node(state)`` and the framework calls
        ``execute(state)`` with no config argument, so there is no per-call
        channel through which configuration could arrive instead.
        """
        self._config: Dict[str, Any] = config or {}
        self._llm = llm

    def get_subgraph(self) -> Any:
        """Instantiate and return the inner domain workflow graph.

        DomainWorkflowGraph is imported lazily (inside the method) to avoid
        circular-import risk at module load time and to match the pattern in
        the nested Cat 2 reference layout.

        _parent_config() forwards the runtime config required by the inner
        domain nodes (system_prompt, report_template_path,
        shift_duration_hours, ...). Those keys live in config/config.yaml
        under the mfg_c2_004 namespace.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._parent_config(), llm=self._llm)

    def extract_input(self, state: AgentState) -> str:
        """Return the string input passed into inner_graph.invoke().

        PreProcessNode screens and redacts the raw user_input and writes the
        result to validated_input. Prefer that; fall back to user_input if
        validated_input is absent (e.g. in unit tests).
        """
        return str(state.get("validated_input") or state.get("user_input") or "")

    def merge_output(self, state: AgentState, sub_result: Dict[str, Any]) -> Dict[str, Any]:
        """Map inner graph sub_result back into the outer state delta.

        sub_result is the dict returned by DomainWorkflowGraph.get_output().
        Returns ONLY changed keys — never the full state.

        Key coupling (designed together with DomainWorkflowGraph.get_output()):
          Inner get_output() emits  → "shift_report", "status", "trace_id", ...
          This merge_output() reads → sub_result.get("shift_report"),
                                      sub_result.get("status")

        shift_report (str | None): final rendered Markdown shift-end report;
          written by ShiftReportRenderNode inside the inner graph.
        status (str | None): terminal AgentStatus value from the inner graph run.

        Additional keys emitted by get_output() (anomaly_highlights, metrics,
        trace_id, correlation_id, node_history) are available in outer state via
        the framework's state merge — extend this dict if future outer nodes need them.
        """
        return {
            "shift_report": sub_result.get("shift_report"),
            # MFG-F1: PostProcessNode (outer post_process slot) reads state.get("result").
            # The inner graph emits the rendered report under "shift_report", so map it to
            # "result" as well — otherwise the final output surfaced by PostProcessNode is
            # always empty.
            "result": sub_result.get("shift_report"),
            "status": sub_result.get("status"),
        }

    def _parent_config(self) -> Dict[str, Any]:
        """Return the runtime config forwarded to the inner workflow graph.

        This is the outer agent's ``config/config.yaml`` mapping, captured at
        construction. Returning an empty mapping here would leave every
        declared runtime value unread and silently degrade the inner nodes to
        their built-in defaults.
        """
        return self._config


class MfgC2004Agent(AgentBaseGraph):
    """Outer graph for MFG-C2-004 (Cat 2).

    Inherits AgentBaseGraph directly (L1 Base). Domain logic is fully
    encapsulated in ShiftReportGraphNode (main slot), which delegates to
    DomainWorkflowGraph (inner BaseGraph).

    Backbone (fixed — identical to Cat 1):
        START → initialize → pre_process → main → post_process → finalize → END

    register_nodes() is the ONLY override:
      - super().register_nodes() fills: initialize, finalize (framework defaults)
      - pre_process: PreProcessNode (input validation + worker-identifier redaction)
      - main:        ShiftReportGraphNode (delegates to DomainWorkflowGraph)
      - post_process: PostProcessNode (output boundary)

    add_edges() is NOT overridden — backbone wiring belongs to the framework.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None, llm: Optional[Any] = None) -> None:
        """Build the agent with its runtime config and optional LLM service.

        ``config`` is the mapping loaded from ``config/config.yaml``.
        ``llm`` implements the framework ``BaseLLM`` contract
        (``complete(messages) -> dict``); when omitted, the narrative section
        of the report is composed deterministically from computed metrics.
        """
        super().__init__(config=config)
        self._llm = llm

    @property
    def name(self) -> str:
        """Agent identifier registered with AgentRegistry."""
        return "mfg_c2_004"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill all 5 backbone slots.

        super().register_nodes() MUST be called first — it injects the
        framework's default InitializeNode (sets schema_version, session_id,
        trust_level) and FinalizeNode (builds response_metadata, total_time_ms).
        """
        super().register_nodes()  # fills: initialize, finalize

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = ShiftReportGraphNode(config=self.config, llm=self._llm)
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.
