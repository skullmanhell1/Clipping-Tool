"""Deterministic transcript and metadata compliance checks."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ComplianceResult:
    passed: bool
    forbidden_hits: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    checked_text_length: int = 0

    @property
    def blockers(self) -> list[str]:
        return [
            *[f"forbidden:{item}" for item in self.forbidden_hits],
            *[f"missing_required:{item}" for item in self.missing_required],
        ]


def _terms(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip().casefold()
        if text and text not in result:
            result.append(text)
    return result


def check_compliance(
    text: str,
    *,
    forbidden_words: Iterable[Any] = (),
    required_words: Iterable[Any] = (),
) -> ComplianceResult:
    """Check whole-word forbidden terms and required phrases case-insensitively."""
    haystack = str(text or "")
    folded = haystack.casefold()
    forbidden_hits = [
        term
        for term in _terms(forbidden_words)
        if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", folded)
    ]
    missing_required = [term for term in _terms(required_words) if term not in folded]
    return ComplianceResult(
        passed=not forbidden_hits and not missing_required,
        forbidden_hits=forbidden_hits,
        missing_required=missing_required,
        checked_text_length=len(haystack),
    )


def check_clip(
    clip: Any,
    *,
    forbidden_words: Iterable[Any] = (),
    required_words: Iterable[Any] = (),
) -> ComplianceResult:
    fields = ("title", "description", "hook_text", "cta", "thumbnail_text", "transcript_text")
    text = " ".join(str(getattr(clip, field, "") or "") for field in fields)
    return check_compliance(
        text,
        forbidden_words=forbidden_words,
        required_words=required_words,
    )
