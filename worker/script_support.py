"""Choosing a caption font that can actually render the text (C21).

Captions were rendered in a Latin display face regardless of what was said. On Arabic, Hebrew,
Chinese, Japanese, Korean or Thai that produces a line of ``.notdef`` boxes - tofu - and **nothing
in the pipeline can see it**: libass reports no error, the ASS file is valid, the encode succeeds,
and the clip record says ``captions``. The only symptom is in the pixels.

Three separate problems, and the first is the one that makes the others worth solving carefully.

**1. ``fc-match`` always answers.** It is a *best match*, not a coverage test:
``fc-match ':lang=ar'`` on the machine this was written on returns ``NotoSans[wdth,wght].ttf``,
which contains no Arabic at all. Asking fontconfig "which font for Arabic" and believing the reply
is how you ship tofu while thinking you handled it. Coverage here is decided by reading the font's
own ``cmap``, and candidate *families* come from ``fc-list :lang=xx``, which returns nothing when
there is nothing - the honest query.

**2. This repository vendors no font for most non-Latin scripts, and that is worth stating.** The
plan's note says "Noto covers CJK", which is true of the Noto *project* and not of the vendored
``NotoSans[wdth,wght].ttf``: CJK lives in Noto Sans CJK, a separate family of around 100 MB per
weight. Measured coverage of what is actually vendored:

======================= ==========================================================
script                  covered by
======================= ==========================================================
Latin                   every vendored face
Cyrillic                Noto Sans, Montserrat, Oswald, Roboto Condensed
Greek                   Noto Sans, Roboto Condensed
Devanagari              Noto Sans, all three Poppins faces
Arabic, Hebrew, Thai    **nothing vendored**
Han, Hiragana, Hangul   **nothing vendored**
======================= ==========================================================

So for those scripts this module's job is not to pick a font - it is to find a *system* font if one
exists and, when one does not, to say so in the clip record instead of rendering boxes. Vendoring
Noto Sans CJK and Noto Sans Arabic is a real fix and a large one; naming the gap is what makes it a
decision rather than a surprise.

**3. Measured wrapping cannot be used on a shaping script.** C6 computes line breaks by summing
per-glyph advance widths. That is a good approximation for Latin and simply wrong for Arabic, where
letters join and a word's rendered width is not the sum of its isolated forms, and for Devanagari
and Thai, where marks reorder and combine. For those scripts the ASS file is emitted with
``WrapStyle: 0`` so libass wraps it itself - less control, but control based on a wrong measurement
is worse than none.

Bidirectional reordering is deliberately not done here. libass applies FriBidi to the text it is
given, so pre-reordering would reverse it twice and produce backwards Arabic.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from config import settings
from worker import language

logger = logging.getLogger(__name__)

#: One representative codepoint per script, used to test a font's ``cmap``.
#:
#: A single codepoint rather than a range: a font that has the script's most basic letter and is
#: missing rarer ones is still overwhelmingly better than one with none of them, and a
#: full-coverage test would reject almost every real font.
SCRIPT_PROBE_CHAR: dict[str, str] = {
    "latin": "A",
    "greek": "\u03b1",
    "cyrillic": "\u0430",
    "arabic": "\u0627",
    "hebrew": "\u05d0",
    "devanagari": "\u0915",
    "han": "\u4f60",
    "hiragana": "\u3042",
    "katakana": "\u30a2",
    "hangul": "\uac00",
    "thai": "\u0e01",
}

#: The fontconfig language tag to search for each script.
SCRIPT_FC_LANG: dict[str, str] = {
    "arabic": "ar",
    "hebrew": "he",
    "han": "zh",
    "hiragana": "ja",
    "katakana": "ja",
    "hangul": "ko",
    "devanagari": "hi",
    "thai": "th",
    "greek": "el",
    "cyrillic": "ru",
}

#: Scripts where per-glyph advance widths do not sum to the rendered width.
#:
#: Arabic letters join, so an isolated-form sum overestimates badly. Devanagari and Thai reorder and
#: stack marks. Hebrew is *not* here: it is RTL but unjoined, so widths add up - the reordering is
#: libass' problem, not the measurement's.
SHAPING_SCRIPTS: frozenset[str] = frozenset({"arabic", "devanagari", "thai"})

#: Scripts written right to left. Recorded so a caller can note it; no reordering happens here.
RTL_SCRIPTS: frozenset[str] = frozenset({"arabic", "hebrew"})


@lru_cache(maxsize=256)
def _font_cmap(path: str) -> frozenset[int]:
    """The codepoints a font file maps, or an empty set if it cannot be read."""
    try:
        from fontTools.ttLib import TTFont

        handle = open(path, "rb")
        try:
            with TTFont(handle, lazy=True, fontNumber=0) as font:
                return frozenset(font.getBestCmap() or {})
        finally:
            handle.close()
    except Exception:
        logger.debug("C21: could not read cmap from %s", path, exc_info=True)
        return frozenset()


def font_covers(path: str | Path, script: str) -> bool:
    """Whether the font at ``path`` maps ``script``'s probe codepoint."""
    probe = SCRIPT_PROBE_CHAR.get(script)
    if not probe:
        return False
    return ord(probe) in _font_cmap(str(path))


@lru_cache(maxsize=1)
def _vendored_faces() -> tuple[tuple[str, str], ...]:
    """``(family_name, path)`` for every vendored font file, from the manifest.

    Read from the manifest rather than by scanning, so the *name libass will be asked for* is the
    one the manifest declares - the C1 lesson: a name that does not resolve is worse than no name.
    Variable fonts are included here, unlike in the A4 picker, because a variable font's cmap is
    still the right answer for "can this render Arabic" and the family name resolves under
    fontconfig even where ``fontsdir`` cannot select a named instance.
    """
    import json

    from worker.captions import FONT_MANIFEST

    try:
        entries = json.loads(FONT_MANIFEST.read_text(encoding="utf-8")).get("fonts") or []
    except Exception:
        return ()
    faces: list[tuple[str, str]] = []
    for entry in entries:
        name = str(entry.get("name") or "").strip()
        filename = str(entry.get("file") or "").strip()
        if not name or not filename:
            continue
        path = FONT_MANIFEST.parent / "fonts" / filename
        if path.is_file():
            faces.append((name, str(path)))
    return tuple(faces)


@lru_cache(maxsize=32)
def _system_families_for(script: str) -> tuple[str, ...]:
    """System font families that fontconfig says cover ``script``'s language, verified by cmap.

    ``fc-list :lang=xx`` is used rather than ``fc-match``, because ``fc-list`` returns *nothing*
    when nothing matches while ``fc-match`` always returns its best guess - and its best guess for
    Arabic on this machine is a font with no Arabic in it. Even so the result is re-verified against
    each file's ``cmap``: a fontconfig language tag means "supports enough of this language", which
    is a looser claim than "has this glyph".
    """
    lang = SCRIPT_FC_LANG.get(script)
    if not lang:
        return ()
    if not _which("fc-list"):
        return ()
    try:
        proc = subprocess.run(
            ["fc-list", f":lang={lang}", "--format", "%{family[0]}\\t%{file}\\n"],  # noqa: S607 - resolved via PATH on purpose; the binary name is operator-configurable
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ()

    families: list[str] = []
    seen: set[str] = set()
    for line in proc.stdout.splitlines():
        family, _sep, path = line.partition("\t")
        family, path = family.strip(), path.strip()
        if not family or not path or family in seen:
            continue
        if font_covers(path, script):
            seen.add(family)
            families.append(family)
    return tuple(families)


def _which(binary: str) -> str | None:
    import shutil

    return shutil.which(binary)


@dataclass(frozen=True)
class ScriptPlan:
    """How to render a caption in the script its text is actually written in (C21)."""

    script: str
    #: The font family to ask libass for.
    font: str
    #: ``True`` when advance-width measurement is invalid for this script, so C6's wrapping must
    #: be skipped and libass left to wrap.
    needs_shaping: bool = False
    rtl: bool = False
    #: A marker for the clip record: the substituted family, or the unrenderable script.
    marker: str = ""

    @property
    def can_render(self) -> bool:
        return not self.marker.startswith("caption_script_unsupported")


def plan_for_text(text: str, requested_font: str) -> ScriptPlan:
    """Choose a font for ``text``, keeping ``requested_font`` when it can render it (C21).

    Resolution order, and why:

    1. **the requested font**, when its ``cmap`` covers the script - a creator's chosen face is a
       brand decision and must not be overridden for a clip that happens to contain one Greek
       letter;
    2. **a vendored face** that covers it, so an offline install still works;
    3. **a system family** that covers it, verified by ``cmap`` rather than trusted from
       fontconfig;
    4. **nothing** - keep the requested font and record ``caption_script_unsupported:<script>``.

    Step 4 is the point of the whole module. The current behaviour in that case is a line of tofu
    boxes that no code can detect, so the honest outcome is to render the same thing *and say so*
    rather than to substitute a font that will not help either.
    """
    script, _share = language.dominant_script(text or "")
    requested = str(requested_font or "").strip()

    if script in ("unknown", "latin", ""):
        # Latin needs no decision, and unknown means punctuation or digits only.
        return ScriptPlan(script=script or "unknown", font=requested)

    needs_shaping = script in SHAPING_SCRIPTS
    rtl = script in RTL_SCRIPTS

    # 1. The requested face, if it can do the job.
    for name, path in _vendored_faces():
        if name.lower() == requested.lower():
            if font_covers(path, script):
                return ScriptPlan(script, requested, needs_shaping, rtl)
            break

    # 2. A vendored face that covers it. Ordered by the manifest, which puts the heavy display
    #    faces first - so a substitution stays as close to the intended look as the coverage allows.
    for name, path in _vendored_faces():
        if font_covers(path, script):
            return ScriptPlan(
                script,
                name,
                needs_shaping,
                rtl,
                marker=f"caption_font_substituted:{script}:{name}",
            )

    # 3. A system family, verified.
    for family in _system_families_for(script):
        return ScriptPlan(
            script,
            family,
            needs_shaping,
            rtl,
            marker=f"caption_font_substituted:{script}:{family}",
        )

    # 4. Nothing can render it. Keep the requested font - substituting a different Latin face would
    #    change the look without fixing anything - and record it.
    return ScriptPlan(
        script,
        requested,
        needs_shaping,
        rtl,
        marker=f"caption_script_unsupported:{script}",
    )


def wrap_style(plan: ScriptPlan) -> int:
    """The ASS ``WrapStyle`` for ``plan``.

    ``2`` is "no automatic wrapping", which is what the rest of this project relies on: C6 inserts
    measured ``\\N`` breaks and needs libass not to second-guess them.

    A shaping script gets ``0`` (libass' own smart wrapping) instead, because the measurement C6
    would be inserting breaks from is not valid there - an Arabic word's rendered width is not the
    sum of its letters' isolated advances. Less control, but control from a wrong number is worse.
    """
    return 0 if plan.needs_shaping else 2


def reset_caches() -> None:
    """Clear the cmap and fontconfig caches. For tests, and after a font is added at runtime."""
    _font_cmap.cache_clear()
    _vendored_faces.cache_clear()
    _system_families_for.cache_clear()


def coverage_report() -> dict[str, list[str]]:
    """``script -> the vendored families that cover it``. For diagnostics and the API.

    Exists because "which scripts can this install caption?" was previously unanswerable without
    rendering a clip and looking at it.
    """
    report: dict[str, list[str]] = {}
    for script in SCRIPT_PROBE_CHAR:
        report[script] = [name for name, path in _vendored_faces() if font_covers(path, script)]
    return report


def unsupported_scripts() -> list[str]:
    """Scripts no vendored *or* system font can render on this machine."""
    missing: list[str] = []
    for script in SCRIPT_PROBE_CHAR:
        if any(font_covers(path, script) for _name, path in _vendored_faces()):
            continue
        if _system_families_for(script):
            continue
        missing.append(script)
    return missing


# `settings` is imported for symmetry with the rest of the worker package and to keep the module
# usable as a standalone diagnostic entry point.
_ = settings
