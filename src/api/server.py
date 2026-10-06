"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, the gateway calls agent.invoke() directly.

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from framework.security.credential_detector import detect_credentials_in_value
from framework.utils.config_loader import load_agent_config
from pydantic import BaseModel, Field
from shared.secrets import factory as secrets_factory

from src.graph.graph import MfgC2004Agent
from src.validation import MAX_PAYLOAD_BYTES, safe_field_name

app = FastAPI(title="Manufacturing Shift Report Agent")

# Runtime values live in config/config.yaml; the manifest (config/agent.yaml)
# stays static. Loading here is what makes the declared values reach the graph.
_AGENT_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = load_agent_config(_AGENT_ROOT)

agent = MfgC2004Agent(config=_CONFIG)
agent.compile()
agent.provision_secrets(secrets_factory(namespace="mfg-c2-004", agent_name="mfg_c2_004"))

# Context keys this adapter forwards. Anything else is DROPPED rather than
# ignored: an undeclared key is not filtered out by a validator further in, it
# simply travels — and the first node returns it verbatim in its result, where
# the framework's output gate scans it.
_ALLOWED_CONTEXT_KEYS = frozenset({"channel", "site_id", "requested_by_role"})

# Serialized size cap for the caller-metadata channel, enforced at the adapter
# so an oversized payload never reaches the graph.
_MAX_INPUT_CONTEXT_BYTES = 256 * 1024


class InvokeRequest(BaseModel):
    """Caller payload for a shift-report request."""

    input: str = Field(..., max_length=MAX_PAYLOAD_BYTES)
    session_id: str = Field(default="", max_length=128)
    input_context: Dict[str, Any] = Field(default_factory=dict)


def _screen_context(context: Dict[str, Any]) -> Optional[str]:
    """Return the name of the first field carrying a credential shape.

    Screening happens here, before invoke(), because a credential-shaped value
    anywhere in input_context fails the run at the FIRST node: the initialize
    step returns input_context verbatim in its result, and the framework's
    output gate scans every value of every result. The caller would receive an
    opaque node-level error with a traceback instead of an actionable refusal.

    The framework's own detector is used so this refusal set matches the
    framework block set exactly. Fields are iterated individually only to name
    the offender — detect_credentials_in_value over a mapping is defined as the
    union across its values, so per-field scanning covers exactly the same set.
    """
    for index, (key, value) in enumerate(context.items(), start=1):
        if detect_credentials_in_value(value):
            name = safe_field_name(key)
            if name == "<masked field name>" or detect_credentials_in_value(key):
                return f"input_context field #{index}"
            return f"input_context.{name}"
    return None


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Dict[str, Any]:
    """Run a shift-report request."""
    context_size = len(json.dumps(req.input_context, ensure_ascii=False, default=str).encode("utf-8"))
    if context_size > _MAX_INPUT_CONTEXT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"input_context exceeds the {_MAX_INPUT_CONTEXT_BYTES}-byte limit",
        )

    # Unknown keys are DROPPED, not merely left unvalidated. A validator that
    # ignores an undeclared key does not remove it: the key still travels into
    # state, is returned verbatim by the first node, and is scanned there.
    context = {k: v for k, v in req.input_context.items() if k in _ALLOWED_CONTEXT_KEYS}

    offending_field = _screen_context(context)
    if offending_field is not None:
        # 400, not 422: pydantic owns 422 and returns a list of error objects
        # there, so reusing it would make client handling ambiguous.
        raise HTTPException(
            status_code=400,
            detail=(f"Request refused: {offending_field} contains a credential-shaped " "value. Remove it and retry."),
        )

    with bound_secrets(agent._secrets_provider):
        # Trust promotion. The manifest declares required_trust_level: INTERNAL,
        # and every domain node enforces it: shift reports carry plant
        # operational data, so the agent is not reachable from an external
        # caller at all. A request presenting the deployment's INVOKE_AUTH_TOKEN
        # is therefore an internal caller by definition of that credential —
        # anything less cannot run the workflow.
        #
        # Unauthenticated requests stay ANONYMOUS and are refused by the trust
        # gate before any domain node runs. A middleware that has already
        # established request.state.trust_level takes precedence, so a platform
        # gateway remains the authority where one is deployed.
        #
        # STG_INTERNAL_RUNNER_TOKEN is the second accepted credential. The
        # Stage-5 evidence harness reads required_trust_level from the manifest
        # and, for an INTERNAL entry, presents THAT token rather than the
        # ordinary bearer (scripts/stg_invoke_evidence.py, "entry_internal").
        # Without this branch the deploy-stg invoke arrives unauthenticated,
        # falls back to ANONYMOUS and is refused by the trust gate — surfacing
        # as `agent_invoke_responsive: false` with nothing to indicate that the
        # credential, not the agent, was the problem. deploy/local-stg.yml
        # already passes the variable; only this adapter did not read it.
        #
        # Deployments must treat BOTH tokens as internal-boundary credentials
        # and terminate them inside the plant network.
        auth_header = request.headers.get("Authorization", "")
        accepted_tokens = (
            os.environ.get("INVOKE_AUTH_TOKEN", ""),
            os.environ.get("STG_INTERNAL_RUNNER_TOKEN", ""),
        )
        if any(token and auth_header == f"Bearer {token}" for token in accepted_tokens):
            trust = TrustLevel.INTERNAL
        else:
            trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)

        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        result: Dict[str, Any] = agent.invoke(req.input, ctx=ctx, input_context=context)
        return result


@app.get("/health")
def health() -> Dict[str, str]:
    """Liveness probe."""
    return {"status": "ok", "agent": "mfg_c2_004"}
