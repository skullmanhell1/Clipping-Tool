"""Focused tests for isolated dynamic campaign preset policy."""

from __future__ import annotations

import re
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from worker import campaign_preset as cp
from worker.campaign_preset import (
    Branding,
    CampaignPreset,
    CliffhangerSettings,
    HookSettings,
    merge_presets,
    parse_campaign_preset,
    plan_daily_slots,
    swap_presets,
    validate_clip_against_preset,
)

BRIEF = """
# Campaign: Spring Launch

## Branding
- Logo: https://cdn.example.test/spring/logo.png
- Font: Brand Sans Bold
- Colors: primary=#112233; accent=#ff00aa

Caption style: Punchy Karaoke
Aspect ratios: 9:16, 1920:1080
Hashtags: #SpringLaunch, #MadeForEveryday
CTA: Shop the new collection
Allowed platforms: TikTok, Instagram Reels
Daily clip target: 3
Public visibility: required
Pattern interrupter interval: every 4 clips
Hook: enabled, 3 seconds, curiosity
Cliffhanger: enabled, 2 seconds, open loop
"""


def test_google_doc_brief_parses_complete_campaign_preset():
    preset = parse_campaign_preset(BRIEF)

    assert preset.name == "Spring Launch"
    assert preset.branding == Branding(
        logo="https://cdn.example.test/spring/logo.png",
        font="Brand Sans Bold",
        colors={"primary": "#112233", "accent": "#ff00aa"},
    )
    assert preset.caption_style == "Punchy Karaoke"
    assert preset.aspect_ratios == ["9:16", "16:9"]
    assert preset.hashtags == ["#SpringLaunch", "#MadeForEveryday"]
    assert preset.cta == "Shop the new collection"
    assert preset.allowed_platforms == ["tiktok", "instagram"]
    assert preset.daily_clip_target == 3
    assert preset.public_visibility_required is True
    assert preset.pattern_interrupter_interval == 4
    assert preset.hook_settings == HookSettings(True, 3.0, "curiosity")
    assert preset.cliffhanger_settings == CliffhangerSettings(True, 2.0, "open loop")


def test_default_daily_target_and_public_policy_are_safe():
    preset = parse_campaign_preset("Campaign: Minimal")

    assert preset.daily_clip_target == 2
    assert preset.require_public_visibility is True


def test_preset_merge_and_swap_are_deterministic_and_do_not_alias_inputs():
    base = CampaignPreset(
        name="Base",
        branding=Branding(logo="base.png", colors={"primary": "#111111"}),
        hashtags=["#base"],
        cta="Learn more",
        daily_clip_target=4,
    )
    override = CampaignPreset(
        branding=Branding(font="Campaign Font", colors={"accent": "#222222"}),
        hashtags=["#launch"],
        cta="Shop now",
    )

    merged = merge_presets(base, override)
    assert merged.branding.logo == "base.png"
    assert merged.branding.font == "Campaign Font"
    assert merged.branding.colors == {"primary": "#111111", "accent": "#222222"}
    assert merged.hashtags == ["#launch"]
    assert merged.cta == "Shop now"
    assert merged.daily_clip_target == 4

    replacement = CampaignPreset(name="Replacement", hashtags=["#new"])
    swapped = swap_presets(base, replacement)
    assert swapped.to_dict() == replacement.to_dict()
    assert swapped is not replacement
    swapped.hashtags.append("#local")
    assert replacement.hashtags == ["#new"]


def test_compliance_report_exposes_failures_and_unknown_evidence():
    preset = CampaignPreset(
        branding=Branding(logo="logo.png"),
        aspect_ratios=["9:16"],
        hashtags=["#launch", "#spring"],
        cta="Shop now",
        allowed_platforms=["tiktok"],
        hook_settings=HookSettings(enabled=True),
    )

    failed = validate_clip_against_preset(
        preset,
        {"visibility": "private", "platform": "youtube", "aspect_ratio": "16:9"},
        {"hashtags": ["#launch"], "cta": "Learn more"},
    )
    assert failed.status == "fail"
    assert failed.for_rule("visibility").status == "fail"
    assert failed.for_rule("platform").status == "fail"
    assert failed.for_rule("aspect_ratio").status == "fail"
    assert failed.for_rule("hashtags").status == "fail"
    assert failed.for_rule("cta").status == "fail"
    assert failed.for_rule("branding").status == "unknown"
    assert failed.for_rule("hook").status == "unknown"

    unknown = validate_clip_against_preset(preset, {}, {})
    assert unknown.status == "unknown"
    assert unknown.for_rule("visibility").status == "unknown"


def test_daily_slots_are_exactly_two_distinct_utc_epochs_and_deterministic():
    slots = plan_daily_slots(date(2026, 3, 5), "America/New_York", [9, "18:30", 21])
    repeated = plan_daily_slots(datetime(2026, 3, 5), "America/New_York", [9, "18:30"])

    assert len(slots) == 2
    assert len(set(slots)) == 2
    assert slots == repeated
    assert slots == (
        datetime(2026, 3, 5, 9, tzinfo=ZoneInfo("America/New_York")).timestamp().__int__(),
        1772753400,
    )


def test_daily_slot_planning_rejects_non_public_visibility():
    with pytest.raises(ValueError, match="public"):
        plan_daily_slots("2026-03-05", "UTC", [9, 18], visibility="unlisted")

    with pytest.raises(ValueError, match="two distinct"):
        plan_daily_slots("2026-03-05", "UTC", [9, 9])


# --- ReDoS hardening (CodeQL py/polynomial-redos) -------------------------------------------
#
# `parse_campaign_preset` runs on operator-supplied text behind `POST /api/campaign-presets/parse`.
# Three of its regexes were polynomial on adversarial input (long runs of spaces or digits), so
# they were replaced by linear-time helpers. These properties pin the helpers to the *exact*
# results of the original regexes, which are reproduced verbatim below as the oracle, so the
# hardening cannot quietly change what a brief parses to.

_ORIGINAL_LINE_RE = re.compile(r"^(?:#+\s*)?([^:=–-]{1,80}?)(?:\s*[:=–-]\s*(.*))?$")
_ORIGINAL_COLOR_RE = re.compile(r"([\w -]+?)\s*[:=]\s*(#[\da-fA-F]{3,8}|[\w-]+)")
_ORIGINAL_DURATION_RE = re.compile(r"[-+]?\d+(?:\.\d+)?\s*(seconds?|secs?|s)\b", re.I)


def _original_split(text: str) -> tuple[str, str] | None:
    match = _ORIGINAL_LINE_RE.match(text)
    if not match:
        return None
    return match.group(1), (match.group(2) or "").strip()


# Lines never contain "\n" (the parser iterates `splitlines()`), so the oracle's `.`/`$` and the
# helper agree only on single lines; the alphabet reflects that.
_LINE_TEXT = st.text(alphabet="ab #:=–-\t*_`•Z", max_size=120)
_COLOR_TEXT = st.text(alphabet="ab_ -:=#f0\t,;xé", max_size=60)
_DURATION_TEXT = st.text(alphabet="12.+- sSecondEC\t9aé_\u017f", max_size=40)


@settings(max_examples=3000, deadline=None)
@given(_LINE_TEXT)
def test_split_label_line_matches_the_original_regex(text):
    expected = _original_split(text)
    actual = cp._split_label_line(text)
    if expected is None or actual is None:
        assert actual == expected
    else:
        # The raw label can differ only in characters `_label` strips (a backed-off "#" or a
        # space); the normalised label and the value are what the parser consumes.
        assert cp._label(actual[0]) == cp._label(expected[0])
        assert actual[1] == expected[1]


@settings(max_examples=3000, deadline=None)
@given(_COLOR_TEXT)
def test_color_pairs_match_the_original_findall(text):
    assert cp._color_pairs(text) == _ORIGINAL_COLOR_RE.findall(text)


@settings(max_examples=3000, deadline=None)
@given(_DURATION_TEXT)
def test_strip_durations_matches_the_original_sub(text):
    assert cp._strip_durations(text) == _ORIGINAL_DURATION_RE.sub("", text)


@pytest.mark.parametrize(
    "line",
    [
        "Colors: " + "a" + " " * 50_000 + "!",  # the space run CodeQL reported for _colors
        "x" + " " * 50_000 + "y",  # and for _line_parts
        "Hook: " + "9" * 50_000 + "x",  # and the digit run for the duration strip
        "Cliffhanger: " + "1." * 25_000 + "q",
    ],
)
def test_adversarial_brief_lines_parse_in_linear_time(line):
    started = time.perf_counter()
    parse_campaign_preset(line)
    assert time.perf_counter() - started < 2.0
