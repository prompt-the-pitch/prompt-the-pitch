"""Verifier: deterministic checks on everything the language agents write.

It catches the two failures that matter on air: an invented number and a text too long
for the screen. Rejected outputs go back to the agent once with the reasons, then fall
back to a template.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_ALWAYS_ALLOWED = {0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 10.0, 11.0, 90.0, 100.0}


_SHARE_KEYS = ("pct", "possession", "share")


def _collect(value: Any, out: set[float], key: str = "") -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        v = float(value)
        # Texts drop the sign ("momentum of 63.9 towards the away side"), so accept |v| too.
        out.update({x for n in (v, abs(v)) for x in (n, round(n), round(n, 1), round(n, 2))})
        if abs(v) <= 1:  # Shares and probability changes are written as percentages.
            out.update({round(abs(v) * 100), round(abs(v) * 100, 1)})
        if 0 <= v <= 100 and any(k in key for k in _SHARE_KEYS):
            # The other side of a possession share is not an invented number (36.1% -> 63.9%).
            out.update({round(100 - v), round(100 - v, 1)})
    elif isinstance(value, str):
        for match in _NUMBER.findall(value):
            out.add(float(match.replace(",", ".")))
    elif isinstance(value, dict):
        for k, v in value.items():
            _collect(v, out, str(k).lower())
    elif isinstance(value, (list, tuple)):
        for v in value:
            _collect(v, out)
    elif isinstance(value, BaseModel):
        _collect(value.model_dump(), out)


def allowed_numbers(*sources: Any) -> set[float]:
    out: set[float] = set(_ALWAYS_ALLOWED)
    for source in sources:
        _collect(source, out)
    return out


def unknown_numbers(text: str, allowed: set[float]) -> list[str]:
    """Numbers in `text` that appear nowhere in the sources (decimal commas accepted)."""
    bad = []
    for raw in _NUMBER.findall(text):
        n = float(raw.replace(",", "."))
        if not any(abs(n - a) <= max(0.051, 0.01 * abs(a)) for a in allowed):
            bad.append(raw)
    return bad


def check_text(fields: dict[str, tuple[str, int]], allowed: set[float]) -> list[str]:
    """fields: name -> (text, max words). Returns human-readable issues."""
    issues = []
    for name, (text, max_words) in fields.items():
        words = len(text.split())
        if words > max_words:
            issues.append(f"{name} has {words} words; the limit is {max_words}.")
        bad = unknown_numbers(text, allowed)
        if bad:
            issues.append(f"{name} contains numbers not found in the input: {', '.join(bad)}.")
    return issues
