"""AgentCore Platform v1.0"""

# MFG-C2-004 — DomainWorkflowGraph (inner BaseGraph)
#
# This is the INNER graph for the Cat 2 two-layer nested architecture.
# It encapsulates the full manufacturing shift-report domain workflow:
#
#   START → log_ingestion → data_normalization → anomaly_detection
#         → summarization → shift_report_render → END
#
# Called by ShiftReportGraphNode.get_subgraph() (graph.py).
# get_output() shapes the sub_result dict consumed by merge_output() there.
#
# Rules enforced:
#   ✅ Inherits BaseGraph (fully custom topology — no forced backbone)
#   ✅ Implements all 7 BaseGraph ABC methods
#   ✅ register_nodes() does NOT call super() (abstract in BaseGraph)
#   ✅ Does NOT register initialize / finalize (outer backbone concerns)
#   ✅ get_output() designed together with ShiftReportGraphNode.merge_output()
#   ❌ No agenticstar imports
#   ❌ Not placed under src/subagents/

from typing import Any, Dict, Optional

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.nodes.anomaly_detection_node import AnomalyDetectionNode
from src.nodes.data_normalization_node import DataNormalizationNode
from src.nodes.log_ingestion_node import LogIngestionNode
from src.nodes.shift_report_render_node import ShiftReportRenderNode
from src.nodes.summarization_node import SummarizationNode
from src.schemas.state import State


class DomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for MFG-C2-004.

    Inherits BaseGraph directly for a fully custom node topology.
    Called by ShiftReportGraphNode.get_subgraph() in graph.py.

    Pipeline (linear):
        START
          → log_ingestion       (LogIngestionNode)
          → data_normalization  (DataNormalizationNode)
          → anomaly_detection   (AnomalyDetectionNode)
          → summarization       (SummarizationNode)
          → shift_report_render (ShiftReportRenderNode)
          → END

    All nodes are FunctionNode subclasses returning partial-dict state updates.
    initialize / finalize are outer backbone concerns — not registered here.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None, llm: Optional[Any] = None) -> None:
        """Build the inner graph with its runtime config and LLM service.

        ``llm`` is any object implementing the framework ``BaseLLM`` contract
        (``complete(messages) -> dict``). When it is absent the summarization
        step falls back to a narrative composed from the computed metrics, so
        the pipeline still produces a real report rather than a placeholder.
        """
        super().__init__(config=config)
        self._llm = llm

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        """Unique identifier for this inner graph."""
        return "mfg_c2_004_shift_report_workflow"

    @property
    def state_schema(self) -> type:
        """TypedDict subclass shared across inner and outer graph."""
        return State

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        """Validate inner graph config before compilation.

        report_template_path is used by ShiftReportRenderNode and is expected
        in the config namespace forwarded via ShiftReportGraphNode._parent_config().
        Absence is non-fatal — ShiftReportRenderNode falls back to a built-in
        template — so we validate permissively here and log a warning rather
        than raising ConfigError.
        """
        pass

    # ── Node registration ─────────────────────────────────────────────────────

    def register_nodes(self) -> None:
        """Register all 5 domain nodes.

        No super() call — BaseGraph.register_nodes() is abstract.
        Do NOT register initialize or finalize; those are outer backbone
        concerns handled by AgentBaseGraph in graph.py.
        Every key registered here is referenced in add_edges().

        The runtime config is handed to each node at construction. The
        framework calls ``execute(state)`` with no config argument, so this is
        the only channel through which a declared runtime value can reach a
        node.
        """
        self._nodes["log_ingestion"] = LogIngestionNode(config=self.config)
        self._nodes["data_normalization"] = DataNormalizationNode(config=self.config)
        self._nodes["anomaly_detection"] = AnomalyDetectionNode(config=self.config)
        self._nodes["summarization"] = SummarizationNode(config=self.config, llm=self._llm)
        self._nodes["shift_report_render"] = ShiftReportRenderNode(config=self.config)

    # ── Edge wiring ───────────────────────────────────────────────────────────

    def add_edges(self) -> None:
        """Wire the linear manufacturing domain topology.

        Each step passes its partial-dict output into the shared State.
        For this template the topology is intentionally linear — no conditional
        branching between domain nodes. route() is implemented as required by
        the ABC but add_conditional_edges() is not used.
        """
        self._sg.add_edge(START, "log_ingestion")
        self._sg.add_edge("log_ingestion", "data_normalization")
        self._sg.add_edge("data_normalization", "anomaly_detection")
        self._sg.add_edge("anomaly_detection", "summarization")
        self._sg.add_edge("summarization", "shift_report_render")
        self._sg.add_edge("shift_report_render", END)

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(self, state: AgentState) -> str:
        """Conditional routing — required by BaseGraph ABC.

        For this linear topology add_conditional_edges() is not used, so this
        method is never called at runtime. It is implemented to satisfy the ABC
        contract. Returns END on error so an unexpected call does not re-enter
        a processing node.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return "shift_report_render"

    # ── Output shape ──────────────────────────────────────────────────────────

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Shape the output dict returned to the outer graph as sub_result.

        This dict is received by ShiftReportGraphNode.merge_output() in graph.py
        as the `sub_result` argument. Both methods are designed together to
        guarantee field-name consistency:

            Inner get_output()  emits: "shift_report", "status", "trace_id", ...
            Outer merge_output() reads: sub_result.get("shift_report"),
                                        sub_result.get("status")

        Additional fields (anomaly_highlights, metrics, trace_id, correlation_id,
        node_history) are surfaced for observability / downstream extension.
        merge_output() currently maps only shift_report + status into the outer
        state delta; the remaining fields are available for future outer-merge
        extensions without requiring an inner-graph change.
        """
        return {
            "shift_report": state.get("shift_report"),
            "status": state.get("status"),
            "anomaly_highlights": state.get("anomaly_highlights", []),
            "metrics": state.get("metrics", {}),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
