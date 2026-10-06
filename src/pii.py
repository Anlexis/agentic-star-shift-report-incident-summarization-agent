"""AgentCore Platform v1.0"""

# MFG-C2-004 — worker-identifier redaction.
#
# Shift logs are produced by MES systems that routinely carry the operator on
# duty. Worker identifiers are personal data and are never needed to compute a
# production metric, so they are removed at the point the payload enters the
# agent and again on every record retained in State.
#
# Redaction is applied at BOTH boundaries deliberately. The text pass runs
# before the payload is stored, so a raw identifier never reaches State even if
# the payload fails to parse; the mapping pass runs on each retained record, so
# an identifier carried in a field the text pass could not attribute is still
# removed before rendering.

from __future__ import annotations

import re
from typing import Any, Dict, Final, List, Tuple

REDACTED: Final[str] = "[REDACTED]"

# Worker / employee / operator identifier forms seen in MES exports.
_PII_PATTERNS: Final[Tuple[re.Pattern[str], ...]] = (
    re.compile(r"\b(?:EMP|WORKER|EMPL|OPR)-?\d{3,8}\b", re.IGNORECASE),
    re.compile(r"\bW-?\d{4,8}\b", re.IGNORECASE),
    re.compile(r"\bemployee[_\s-]?(?:id|no|number)[:\s=]+\S+", re.IGNORECASE),
    re.compile(r"\bworker[_\s-]?(?:id|no|number)[:\s=]+\S+", re.IGNORECASE),
    re.compile(r"\boperator[_\s-]?(?:id|no|number)[:\s=]+\S+", re.IGNORECASE),
    re.compile(r"\bbadge[_\s-]?(?:id|no|number)?[:\s=]+\S+", re.IGNORECASE),
)

# Keys whose VALUE is a worker identifier regardless of its shape.
_PII_KEYS: Final[frozenset[str]] = frozenset(
    {
        "worker_id",
        "employee_id",
        "operator_id",
        "badge_id",
        "worker_no",
        "employee_no",
        "operator_no",
        "badge_no",
        "worker_name",
        "employee_name",
        "operator_name",
        "operator",
    }
)


def strip_pii_text(value: str) -> str:
    """Replace worker-identifier forms in a string with the redaction marker."""
    if not isinstance(value, str):
        return value
    for pattern in _PII_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def strip_pii_mapping(record: Dict[str, Any], _depth: int = 0) -> Dict[str, Any]:
    """Return a copy of ``record`` with worker identifiers removed.

    Recurses into nested mappings and lists: an identifier one level down is
    the same disclosure as one at the top level.
    """
    if _depth > 12:
        return {}
    sanitized: Dict[str, Any] = {}
    for key, value in record.items():
        if isinstance(key, str) and key.lower() in _PII_KEYS:
            sanitized[key] = REDACTED
        elif isinstance(value, str):
            sanitized[key] = strip_pii_text(value)
        elif isinstance(value, dict):
            sanitized[key] = strip_pii_mapping(value, _depth + 1)
        elif isinstance(value, list):
            sanitized[key] = _strip_pii_sequence(value, _depth + 1)
        else:
            sanitized[key] = value
    return sanitized


def _strip_pii_sequence(values: List[Any], depth: int) -> List[Any]:
    """Apply redaction across a list, preserving order and non-string items."""
    if depth > 12:
        return []
    cleaned: List[Any] = []
    for item in values:
        if isinstance(item, str):
            cleaned.append(strip_pii_text(item))
        elif isinstance(item, dict):
            cleaned.append(strip_pii_mapping(item, depth + 1))
        elif isinstance(item, list):
            cleaned.append(_strip_pii_sequence(item, depth + 1))
        else:
            cleaned.append(item)
    return cleaned


def contains_pii(value: str) -> bool:
    """Return True when a worker-identifier form is present in ``value``."""
    if not isinstance(value, str):
        return False
    return any(pattern.search(value) for pattern in _PII_PATTERNS)
