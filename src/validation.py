"""AgentCore Platform v1.0"""

# MFG-C2-004 — caller-input validation helpers.
#
# Every value in a shift-log payload arrives from the caller and is therefore
# untrusted. The helpers here enforce three properties before any caller value
# is stored in State or rendered into a report:
#
#   1. Numbers are finite and bounded. ``float("nan")`` and ``float("inf")``
#      parse successfully but compare False against every threshold, so an
#      unchecked non-finite value silently disables the exact comparison this
#      agent exists to make. Parsing fails closed instead.
#   2. Identifiers that render into the report are inert. Machine, line,
#      product and shift identifiers are echoed into the shift report, so they
#      are restricted to a fixed alphabet rather than carried as free text.
#   3. Free text is screened for prompt-injection payloads, including
#      chat-template control tokens, both before and after markup stripping.
#
# Rejections name the offending field and never echo the rejected value.

from __future__ import annotations

import math
import re
from typing import Any, Dict, Final, Iterable, Optional

# --------------------------------------------------------------------------
# Structural caps
# --------------------------------------------------------------------------

# Largest accepted serialized shift-log payload (bytes). Mirrors the adapter cap.
MAX_PAYLOAD_BYTES: Final[int] = 256 * 1024

# Hard ceiling on log entries per shift, independent of the configured value.
MAX_LOG_RECORDS_CEILING: Final[int] = 50_000

# Longest accepted free-text note carried on a single log entry.
MAX_NOTE_CHARS: Final[int] = 500

# Longest accepted identifier (machine / line / product / shift / alarm code).
MAX_IDENTIFIER_CHARS: Final[int] = 32

# Magnitude ceiling for any caller-supplied numeric quantity.
MAX_NUMERIC_MAGNITUDE: Final[float] = 1e12


class InputRejected(ValueError):
    """Raised when a caller-supplied value fails validation.

    Carries the offending field NAME only. The value itself is never stored on
    the exception, so it cannot reach a log line or an error envelope.
    """

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


# --------------------------------------------------------------------------
# Numeric validation — fail closed
# --------------------------------------------------------------------------


def finite_in_range(
    value: Any,
    *,
    field: str,
    minimum: float,
    maximum: float,
    default: Optional[float] = None,
) -> float:
    """Parse ``value`` as a finite float within ``[minimum, maximum]``.

    Rejects booleans (``True`` is not a quantity), non-numeric strings, and the
    non-finite floats NaN / +Inf / -Inf, which arrive both via ``float("nan")``
    and as bare ``NaN`` / ``Infinity`` tokens in raw JSON.

    ``default`` is returned only when ``value`` is absent (``None``). A present
    but invalid value is always an error — absence and malformation are
    different situations and only the first has a safe fallback.
    """
    if value is None:
        if default is None:
            raise InputRejected(field, "required numeric value is missing")
        return float(default)

    # bool is a subclass of int; a flag is never a measurement.
    if isinstance(value, bool):
        raise InputRejected(field, "expected a number, got a boolean")

    candidate: Any
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            raise InputRejected(field, "expected a number, got an empty string")
    elif isinstance(value, (int, float)):
        candidate = value
    else:
        raise InputRejected(field, f"expected a number, got {type(value).__name__}")

    try:
        parsed = float(candidate)
    except (TypeError, ValueError):
        raise InputRejected(field, "value is not a valid number") from None

    if not math.isfinite(parsed):
        raise InputRejected(field, "value must be finite (NaN and Infinity are rejected)")

    if abs(parsed) > MAX_NUMERIC_MAGNITUDE:
        raise InputRejected(field, "value magnitude exceeds the accepted ceiling")

    if parsed < minimum or parsed > maximum:
        raise InputRejected(field, "value is outside the accepted range")

    return parsed


# --------------------------------------------------------------------------
# Identifier validation — inert by construction
# --------------------------------------------------------------------------

# The render alphabet for every caller identifier echoed into a shift report.
# Manufacturing identifiers are alphanumeric with hyphens and underscores
# (bearing codes such as SKF-6205, line codes such as LINE_02, shift codes such
# as SH-2026-08-31-A). Anything outside this class is caller-controlled output,
# not an identifier.
_INERT_IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(r"\A[A-Za-z0-9_-]{1," + str(MAX_IDENTIFIER_CHARS) + r"}\Z")


def inert_identifier(
    value: Any,
    *,
    field: str,
    default: str = "",
    required: bool = False,
) -> str:
    """Return ``value`` if it is an inert identifier, else fail closed.

    An identifier that does not match the render alphabet is rejected rather
    than sanitized: silently rewriting a machine or part identifier would
    change what the report says about a physical asset, which is worse than
    refusing the record.
    """
    if value is None or value == "":
        if required:
            raise InputRejected(field, "required identifier is missing")
        return default

    if not isinstance(value, str):
        raise InputRejected(field, f"expected an identifier string, got {type(value).__name__}")

    if not _INERT_IDENTIFIER_RE.match(value):
        raise InputRejected(
            field,
            "identifier must be 1-%d characters of letters, digits, '-' or '_'" % MAX_IDENTIFIER_CHARS,
        )

    return value


def safe_field_name(name: Any) -> str:
    """Render a caller-supplied field NAME safely for an error message.

    Field names are caller data too. A name that is not itself inert is
    replaced with a positional description rather than echoed.
    """
    if isinstance(name, str) and re.match(r"\A[A-Za-z0-9_.-]{1,64}\Z", name):
        return name
    return "<masked field name>"


# --------------------------------------------------------------------------
# Prompt-injection screening
# --------------------------------------------------------------------------

# Chat-template control tokens, screened as a CLASS rather than as individual
# literals: the payload that matters is structural (it terminates the current
# turn and opens a new one), and a screen that matches only known phrases
# misses it entirely.
_CONTROL_TOKEN_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"<\|[^|>]{0,64}\|>"),  # <|im_start|>, <|endoftext|>, ...
    re.compile(r"\[/?INST\]", re.IGNORECASE),  # [INST] / [/INST]
    re.compile(r"<</?SYS>>", re.IGNORECASE),  # <<SYS>> / <</SYS>>
    re.compile(r"<\|?im_(?:start|end)\|?>", re.IGNORECASE),
)

# Directive phrases. Both ends are anchored to a phrase shape rather than to a
# bare verb: manufacturing log text legitimately contains words like "stop",
# "override" and "reset", and an unanchored verb screen would refuse real
# maintenance notes.
_DIRECTIVE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|the\s+|your\s+|"
        r"previous\s+|prior\s+|above\s+)*(?:instruction|rule|direction|prompt|"
        r"guideline|constraint)s?\b",
        re.IGNORECASE,
    ),
    re.compile(r"\byou\s+are\s+now\s+(?:a|an|the)\b", re.IGNORECASE),
    re.compile(r"\b(?:system|developer)\s+prompt\b", re.IGNORECASE),
    re.compile(r"\bact\s+as\s+(?:a|an|the)\s+(?:different|new)\b", re.IGNORECASE),
    re.compile(r"\breveal\s+(?:your|the)\s+(?:system\s+)?(?:prompt|instruction)s?\b", re.IGNORECASE),
)

# Markup strip used only to re-screen spliced directives. Stripping markup is
# NOT itself a defence: it can convert a detectable control token into
# undetectable plain text, so the raw form is always screened first.
_MARKUP_RE: Final[re.Pattern[str]] = re.compile(r"<[^>]{0,200}>")


def screen_text(value: Any, *, field: str) -> str:
    """Screen caller free text for injection payloads; fail closed.

    The value is screened TWICE — once raw, so control tokens are caught
    before any stripping can remove them, and once with markup removed, so a
    directive spliced across tags (``ig<b>nore all instructions``) is caught
    after the surrounding text re-assembles.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InputRejected(field, f"expected text, got {type(value).__name__}")

    for pattern in _CONTROL_TOKEN_PATTERNS:
        if pattern.search(value):
            raise InputRejected(field, "text contains a chat-template control token")

    stripped = _MARKUP_RE.sub("", value)
    for candidate in (value, stripped):
        for pattern in _DIRECTIVE_PATTERNS:
            if pattern.search(candidate):
                raise InputRejected(field, "text contains an instruction-override directive")

    return value


def screen_structure(value: Any, *, field: str, _depth: int = 0) -> None:
    """Depth-first screen of a parsed payload, KEYS included.

    Screening after parsing rather than on the raw string is what makes
    ``\\u``-escaped payloads visible: by this point the escapes have been
    decoded by the JSON parser, so an escaped control token is an ordinary
    string here.
    """
    if _depth > 12:
        raise InputRejected(field, "payload nesting exceeds the accepted depth")

    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                screen_text(key, field=f"{field}.<key {safe_field_name(key)}>")
            screen_structure(item, field=f"{field}.{safe_field_name(key)}", _depth=_depth + 1)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            screen_structure(item, field=f"{field}[{index}]", _depth=_depth + 1)
    elif isinstance(value, str):
        screen_text(value, field=field)


def bounded_note(value: Any, *, field: str) -> str:
    """Screen and length-cap a free-text note carried on a log entry."""
    text = screen_text(value, field=field)
    return text[:MAX_NOTE_CHARS]


def domain_config(config: Optional[Dict[str, Any]], *, keys: Iterable[str] = ()) -> Dict[str, Any]:
    """Return the MFG-C2-004 runtime namespace from a graph config mapping.

    ``config`` is the mapping loaded from ``config/config.yaml`` and handed to
    the graph constructor. ``keys`` is accepted for call-site readability only;
    the whole namespace is returned.
    """
    del keys
    if not isinstance(config, dict):
        return {}
    namespace = config.get("mfg_c2_004")
    return namespace if isinstance(namespace, dict) else {}
