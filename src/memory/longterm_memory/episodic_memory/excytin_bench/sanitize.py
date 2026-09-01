"""Deterministic redaction helpers for retrievable episodic knowledge."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from sqlglot import exp

_TRUNCATION_RE = re.compile(r"(?:\s*\.\.\. \(Truncated\))+\s*$", re.IGNORECASE)
_SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("URL", re.compile(r"https?://[^\s'\"`<>()\[\]{}]+", re.IGNORECASE)),
    (
        "EMAIL",
        re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    ),
    ("SID", re.compile(r"\bS-\d(?:-\d+){2,}\b", re.IGNORECASE)),
    (
        "UUID",
        re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            re.IGNORECASE,
        ),
    ),
    (
        "TIMESTAMP",
        re.compile(
            r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "IP",
        re.compile(
            r"(?<![\w:])(?:25[0-5]|2[0-4]\d|1?\d?\d)"
            r"(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![\w:])"
        ),
    ),
    ("HASH", re.compile(r"\b[0-9a-f]{32,64}\b", re.IGNORECASE)),
)


def iter_scalar_strings(value: Any) -> Iterable[str]:
    """Yield scalar strings from arbitrarily nested benchmark answer data."""

    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            yield stripped
    elif isinstance(value, dict):
        for nested in value.values():
            yield from iter_scalar_strings(nested)
    elif isinstance(value, (list, tuple, set)):
        for nested in value:
            yield from iter_scalar_strings(nested)
    elif value is not None and not isinstance(value, bool):
        yield str(value)


def sanitize_text(text: str, *, protected_values: Iterable[str] = ()) -> str:
    """Remove known answers and common security-entity values from text."""

    sanitized = text
    values = sorted(
        {value.strip() for value in protected_values if value and len(value.strip()) >= 3},
        key=len,
        reverse=True,
    )
    for index, value in enumerate(values, start=1):
        sanitized = re.sub(re.escape(value), f"<PROTECTED_{index}>", sanitized, flags=re.IGNORECASE)

    for label, pattern in _SENSITIVE_PATTERNS:
        counter = 0

        def replace(_: re.Match[str], token_label: str = label) -> str:
            nonlocal counter
            counter += 1
            return f"<{token_label}_{counter}>"

        sanitized = pattern.sub(replace, sanitized)

    return " ".join(sanitized.split())


def normalize_error_detail(observation: str) -> str:
    """Retain the reusable database error while removing transport truncation noise."""

    return _TRUNCATION_RE.sub("", observation).strip()


def classify_literal(value: str) -> str:
    """Return a stable placeholder type for a SQL string literal."""

    for label, pattern in _SENSITIVE_PATTERNS:
        if pattern.fullmatch(value.strip()):
            return label
    return "STRING"


def parameterize_sql(tree: exp.Expression) -> tuple[str, list[str]]:
    """Replace business literals in a parsed SQL AST with typed placeholders."""

    counters: dict[str, int] = {}
    parameter_types: list[str] = []

    def replace(node: exp.Expression) -> exp.Expression:
        if not isinstance(node, exp.Literal):
            return node
        if isinstance(node.parent, (exp.Limit, exp.Offset)):
            return node

        label = classify_literal(node.this) if node.is_string else "NUMBER"
        counters[label] = counters.get(label, 0) + 1
        placeholder = f"<{label}_{counters[label]}>"
        parameter_types.append(label)
        if node.is_string:
            return exp.Literal.string(placeholder)
        return exp.Var(this=placeholder)

    parameterized = tree.copy().transform(replace)
    return parameterized.sql(dialect="mysql"), parameter_types
