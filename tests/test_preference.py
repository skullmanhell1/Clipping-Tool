"""Pairwise preference harness (M12).

The harness is the instrument, so these tests attack the controls rather than any render: blinding,
per-trial randomisation, declines surviving the tally, and the report refusing to claim
significance.

`Property 1` is the one that needs a property test rather than an example: presentation order must
be randomised **across trials** and must not be a fixed function of the configuration. A set-level
coin flip satisfies "randomised" in the loosest sense while reintroducing exactly the position bias
the control exists to remove, and a single example cannot tell the two apart.
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from evaluation import preference as pref
from evaluation.preference import CHOICE_A, CHOICE_B, DECLINE, Judgement


def _pairs(n: int = 8) -> list[tuple[str, str]]:
    return [(f"base_{i}.mp4", f"cand_{i}.mp4") for i in range(n)]


# --- 6.6 / Property 1: randomisation --------------------------------------------------------


# Feature: render-quality-measurement, Property 1: presentation order is randomised across
# trials and is not a fixed function of the configuration.
@settings(max_examples=100, deadline=None)
@given(seed=st.integers(min_value=0, max_value=10_000))
def test_property_presentation_order_is_not_a_fixed_function_of_configuration(seed):
    """Both configurations must appear in slot A across a set of trials.

    A set-level coin flip would put every baseline in the same slot — still "randomised" if you
    only look at one set, and still position-biased. Drawing per trial is what makes the bias
    average out, and over 24 trials the chance of a correct implementation putting them all in one
    slot is about 1 in 8 million.
    """
    built = pref.build_set("grade", _pairs(24), seed=seed)
    slots = {t.config_a for t in built.trials}
    assert slots == {"baseline", "candidate"}, (
        f"seed {seed}: only {slots} appeared in slot A; order is not randomised per trial"
    )


def test_the_same_seed_reproduces_the_same_set():
    """Reproducibility, so a set someone has already judged can be regenerated."""
    a = pref.build_set("pacing", _pairs(), seed=7)
    b = pref.build_set("pacing", _pairs(), seed=7)
    assert [t.to_dict() for t in a.trials] == [t.to_dict() for t in b.trials]


def test_different_seeds_generally_produce_different_orders():
    """Otherwise the seed is decorative and every set carries the same arrangement."""
    orders = {
        tuple(t.config_a for t in pref.build_set("x", _pairs(12), seed=s).trials)
        for s in range(8)
    }
    assert len(orders) > 1


# --- 6.1 / R5.2: blinding -------------------------------------------------------------------


def test_the_presented_view_contains_no_configuration_names():
    """R5.2. Knowing which is "the new one" produces the expected answer."""
    built = pref.build_set("framing", _pairs(), seed=1)
    blob = json.dumps(built.presented())
    assert "baseline" not in blob
    assert "candidate" not in blob
    for trial in built.presented()["trials"]:
        assert set(trial) == {"trial_id", "a", "b"}


def test_the_written_page_and_trial_list_leak_nothing(tmp_path):
    """The artefacts a judge actually opens.

    Asserted on the files rather than on the data structure, because the leak that matters is the
    one on disk — a judge reads `index.html`, not `Preference_Set.presented()`.
    """
    built = pref.build_set("framing", _pairs(4), baseline="v0.11.0", candidate="new_scaler", seed=3)
    written = pref.write_session(built, tmp_path)

    for name in ("page", "trials"):
        text = written[name].read_text(encoding="utf-8")
        assert "v0.11.0" not in text, f"{name} leaks the baseline's name"
        assert "new_scaler" not in text, f"{name} leaks the candidate's name"

    # The key is where the mapping lives, and it must actually contain it.
    key = json.loads(written["key"].read_text(encoding="utf-8"))
    assert key["baseline"] == "v0.11.0"
    assert key["candidate"] == "new_scaler"


def test_the_page_offers_no_difference_as_a_first_class_button(tmp_path):
    """R5.4. If declining is harder than choosing, the data will show a preference that is fatigue."""
    built = pref.build_set("grade", _pairs(2), seed=1)
    page = pref.write_session(built, tmp_path)["page"].read_text(encoding="utf-8")
    assert DECLINE in page
    assert "No visible difference" in page
    assert "real answer" in page, "the instruction must tell the judge that declining is valid"


def test_the_session_is_entirely_local(tmp_path):
    """R5.8, R6.6. A preference run is a directory of clip pairs.

    Shipping it to a hosted service would make it a dependency and a privacy question for no
    benefit, so the page must reference nothing external.
    """
    built = pref.build_set("grade", _pairs(2), seed=1)
    page = pref.write_session(built, tmp_path)["page"].read_text(encoding="utf-8")
    for forbidden in ("http://", "https://", "cdn", "src=\"//"):
        assert forbidden not in page, forbidden


# --- 6.2 / R5.7: one dimension per set ------------------------------------------------------


def test_a_set_must_name_its_dimension():
    """R5.7. Judging an accumulation of changes tells you nothing about any of them."""
    with pytest.raises(ValueError, match="single dimension"):
        pref.build_set("", _pairs())
    with pytest.raises(ValueError, match="single dimension"):
        pref.build_set("   ", _pairs())


def test_the_dimension_travels_on_every_trial_and_on_the_tally():
    """So a results file cannot be read without knowing what was being judged."""
    built = pref.build_set("caption_line_breaking", _pairs(3), seed=2)
    assert all(t.dimension == "caption_line_breaking" for t in built.trials)
    result = pref.tally(built, [])
    assert result["dimension"] == "caption_line_breaking"


# --- 6.3 / R5.4: declines are data ----------------------------------------------------------


def test_declines_are_counted_and_not_dropped():
    """R5.4. Discarding them manufactures a preference from nothing."""
    built = pref.build_set("zoom", _pairs(4), seed=5)
    judgements = [
        Judgement(built.trials[0].trial_id, CHOICE_A),
        Judgement(built.trials[1].trial_id, DECLINE),
        Judgement(built.trials[2].trial_id, DECLINE),
        Judgement(built.trials[3].trial_id, DECLINE),
    ]
    result = pref.tally(built, judgements)
    assert result["no_difference"] == 3
    assert result["trials_judged"] == 4
    assert sum(result["counts"].values()) == 1
    assert "no visible difference" in result["interpretation"]


def test_a_set_where_nobody_saw_a_difference_reports_that_clearly():
    """The most useful outcome for a change that costs render time.

    It must not be reported as an even split or as an absence of data: "four people looked and
    could not tell" is a finding, and it argues against paying the cost.
    """
    built = pref.build_set("scaler", _pairs(4), seed=5)
    result = pref.tally(built, [Judgement(t.trial_id, DECLINE) for t in built.trials])
    assert result["no_difference"] == 4
    assert result["counts"] == {"baseline": 0, "candidate": 0}
    assert result["trials_judged"] == 4


def test_an_invalid_choice_is_rejected_at_construction():
    """A typo'd choice must not silently become a decline or a vote."""
    with pytest.raises(ValueError, match="choice must be one of"):
        Judgement("t-000", "prefer_a")


# --- the tally resolves through the answer key ----------------------------------------------


def test_a_vote_is_resolved_through_the_answer_key_not_the_slot():
    """The point of blinding: `a` means "the left one", which is a different configuration
    per trial.

    Constructed so the two trials have opposite arrangements, and both judgements choose slot A.
    A tally that counted slots rather than configurations would report 2-0; the correct answer is
    1-1.
    """
    built = pref.build_set("framing", _pairs(12), seed=11)
    first_baseline = next(t for t in built.trials if t.config_a == "baseline")
    first_candidate = next(t for t in built.trials if t.config_a == "candidate")

    result = pref.tally(
        built,
        [
            Judgement(first_baseline.trial_id, CHOICE_A),
            Judgement(first_candidate.trial_id, CHOICE_A),
        ],
    )
    assert result["counts"]["baseline"] == 1
    assert result["counts"]["candidate"] == 1


def test_choosing_slot_b_resolves_to_the_other_configuration():
    built = pref.build_set("framing", _pairs(6), seed=13)
    trial = next(t for t in built.trials if t.config_a == "baseline")
    result = pref.tally(built, [Judgement(trial.trial_id, CHOICE_B)])
    assert result["counts"]["candidate"] == 1
    assert result["counts"]["baseline"] == 0


def test_a_judgement_for_an_unknown_trial_is_reported_not_ignored():
    """Silently dropping it would let a mismatched results file look like a clean small run."""
    built = pref.build_set("framing", _pairs(2), seed=1)
    result = pref.tally(built, [Judgement("not-a-real-trial", CHOICE_A)])
    assert result["unknown_trial_ids"] == ["not-a-real-trial"]
    assert result["trials_judged"] == 0


# --- 6.4 / R5.5, R5.6, R5.9: what the report may and may not claim --------------------------


def test_the_report_refuses_to_claim_significance():
    """R5.5, R5.6. A 4-2 split is noise and the report must not imply otherwise.

    No p-value is computed anywhere in this module, deliberately: with a handful of trials and
    usually one judge, a significance claim would be false precision dressed as rigour.
    """
    built = pref.build_set("grade", _pairs(6), seed=17)
    judgements = []
    for index, trial in enumerate(built.trials):
        judgements.append(Judgement(trial.trial_id, CHOICE_A if index < 4 else CHOICE_B))
    result = pref.tally(built, judgements)

    blob = json.dumps(result).lower()
    for forbidden in ("p-value", "p_value", "significant", "confidence interval"):
        assert forbidden not in blob, forbidden
    assert "cannot distinguish a real preference from noise" in result["interpretation"]
    assert "noise" in result["interpretation"]


def test_a_small_run_is_explicitly_labelled_small():
    built = pref.build_set("grade", _pairs(6), seed=19)
    result = pref.tally(built, [Judgement(t.trial_id, CHOICE_A) for t in built.trials])
    assert f"Fewer than {pref.SMALL_TRIAL_COUNT} trials" in result["interpretation"]


def test_zero_judgements_is_not_reported_as_equivalence():
    """"Nobody judged it" and "nobody could tell" are different findings."""
    built = pref.build_set("grade", _pairs(3), seed=1)
    result = pref.tally(built, [])
    assert result["trials_judged"] == 0
    assert "Nothing can be concluded" in result["interpretation"]
    assert "not evidence that the two renders are equivalent" in result["interpretation"]


def test_judge_count_and_authorship_are_reported():
    """R5.9. Not forbidden — usually unavoidable here — but a reader must be able to discount it."""
    built = pref.build_set("grade", _pairs(3), seed=1)
    result = pref.tally(
        built,
        [
            Judgement(built.trials[0].trial_id, CHOICE_A, judge="alex", authored_the_change=True),
            Judgement(built.trials[1].trial_id, CHOICE_B, judge="sam"),
        ],
    )
    assert result["distinct_judges"] == 2
    assert result["judge_authored_the_change"] is True


def test_authorship_is_false_when_nobody_declared_it():
    built = pref.build_set("grade", _pairs(2), seed=1)
    result = pref.tally(built, [Judgement(built.trials[0].trial_id, CHOICE_A, judge="sam")])
    assert result["judge_authored_the_change"] is False
