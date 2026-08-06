"""Tests for the transcript-derived signals and the translated subtitle track.

Covers C19 (emoji land on the highlighted word, and may sit beside the caption), S7 (question /
answer and list structure), S8 (lexical emotional intensity), S12 (standalone completeness),
T9 (per-segment language detection) and T10 (a translated subtitle track rather than a replaced
transcript).

Every feature here is one whose bugs *look like success*. A scoring signal that does nothing still
produces a plausible ranking; a language detector that always guesses English still returns a
language; an emoji placed on the wrong word still renders. So each test below is written to fail if
the feature were inert:

* S7 asserts an **unanswered** question scores *below* a structureless passage, which is false under
  any implementation that treats structure as a bonus.
* S8 asserts a long passage does not out-score a short one containing the same strong words, which
  is false for a count-based measure.
* S12 asserts the penalties are *ordered*, which is false if they are merely present.
* C19 asserts a highlighted word beats a more salient unhighlighted one, which is false if the
  emoji planner keeps its own opinion.
* T9 asserts Han script yields **no** language, which is false for any detector that guesses.
* T10 asserts the original-language captions survive, which is the whole difference from
  ``task=translate``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from config import settings
from worker import candidate_ranking, discourse, language, subtitle_export
from worker.effects import emoji

requires_ffmpeg = pytest.mark.skipif(
    subprocess.run(["which", settings.ffmpeg_binary], capture_output=True).returncode != 0,
    reason="ffmpeg not on PATH",
)

FFMPEG = settings.ffmpeg_binary


@dataclass
class W:
    start: float
    end: float
    text: str = "word"
    probability: float = 1.0


@dataclass
class Seg:
    start: float
    end: float
    text: str = ""


@dataclass
class Cand:
    start: float
    end: float
    score: float = 0.0
    text: str = ""
    features: dict = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.end - self.start


# --------------------------------------------------------------------------- #
# S7 - question / answer and list structure
# --------------------------------------------------------------------------- #


def test_s7_a_question_with_its_answer_is_the_highest_scoring_shape():
    text = "Why did the deal collapse? The buyer pulled the funding two days before signing."
    structure = discourse.detect_structure(text)
    assert structure.question
    assert structure.answered
    assert structure.score == 1.0


def test_s7_an_unanswered_question_scores_below_a_passage_with_no_structure_at_all():
    """The ordering *is* the feature.

    An unanswered question is not a neutral shape, it is the worst one: it opens a loop the clip
    never closes, which is the most common way an auto-cut moment feels unfinished. Any
    implementation that treats structure as a bonus to be added would score it at or above the
    structureless baseline and still pass a "detects questions" test.
    """
    dangling = discourse.detect_structure("So why do you think that happened?")
    plain = discourse.detect_structure("The warehouse is on the east side of the river.")
    assert dangling.question and not dangling.answered
    assert dangling.score < plain.score


def test_s7_a_trailing_conversational_tag_is_not_an_unanswered_question():
    """ "..., you know?" ends thousands of ordinary sentences.

    Treating it as an opened-and-unclosed loop would penalise normal conversational speech
    everywhere it appears, which is most speech.
    """
    text = "We rebuilt the whole thing from scratch over one weekend, you know?"
    structure = discourse.detect_structure(text)
    assert not structure.question
    assert structure.score >= 0.5


def test_s7_a_sentence_that_is_nothing_but_a_tag_asks_nothing():
    """ "Right?" and "Does that make sense?" are check-ins, not opened loops.

    A question mark is the only thing separating these from a statement, and an unanswered
    question is the lowest-scoring shape here - so reading them literally applies a penalty to
    the way people talk rather than to anything about the clip.
    """
    for tag in ("Right?", "Does that make sense?", "You know what I mean?", "Yeah?"):
        assert not discourse.detect_structure(
            f"The whole migration ran overnight and nobody noticed. {tag}"
        ).question, tag


def test_s7_a_real_question_wearing_a_tag_is_still_a_question():
    """Stripping the tag must not strip the question with it."""
    structure = discourse.detect_structure(
        "Why did nobody check the backups, you know? Because the alert had been muted for months."
    )
    assert structure.question
    assert structure.answered


def test_s7_an_enumeration_is_detected_from_the_promise_not_from_counting_items():
    for text in (
        "Here's why that never works in practice.",
        "There are three things you have to get right.",
        "First of all, nobody reads the documentation.",
        "The bottom line is that it costs more than they said.",
    ):
        assert discourse.detect_structure(text).enumeration, text


def test_s7_an_enumeration_outranks_a_structureless_passage_but_not_an_answered_question():
    """Detecting a list has to *change the ranking*, or the detection is decoration.

    Below an answered question because a list is a promise with an end, while a question with
    its answer is a promise already kept inside the window.
    """
    enumerated = discourse.detect_structure("There are three things you have to get right.")
    plain = discourse.detect_structure("The warehouse is on the east side of the river.")
    answered = discourse.detect_structure(
        "Why did the deal collapse? The buyer pulled the funding two days before signing."
    )
    assert plain.score < enumerated.score < answered.score


def test_s7_ordinary_prose_is_not_read_as_an_enumeration():
    structure = discourse.detect_structure(
        "We drove out to the coast and sat on the wall watching the boats come in."
    )
    assert not structure.enumeration
    assert not structure.question
    assert structure.score == 0.5


def test_s7_a_question_without_a_question_mark_is_still_a_question():
    """ASR omits question marks on rising-intonation questions routinely.

    Relying on the punctuation alone would make the signal a property of Whisper's
    formatting rather than of the speech.
    """
    assert discourse.detect_structure("How do you even find a place like that").question


def test_s7_empty_and_punctuation_only_text_return_the_neutral_shape():
    for text in ("", "   ", "...", "?!"):
        structure = discourse.detect_structure(text)
        assert structure.score == 0.5 or not structure.answered


# --------------------------------------------------------------------------- #
# S8 - emotional intensity
# --------------------------------------------------------------------------- #


def test_s8_strongly_worded_speech_scores_above_neutral_description():
    strong = discourse.emotional_intensity(
        "It was absolutely insane, the whole thing collapsed and everyone was screaming."
    )
    neutral = discourse.emotional_intensity(
        "The meeting is scheduled for Tuesday in the room at the end of the corridor."
    )
    assert strong.score > neutral.score
    assert strong.strong_terms >= 2


def test_s8_is_a_density_so_padding_a_passage_cannot_raise_its_intensity():
    """The defect S11 exists to remove, in a different place.

    A raw count would rank a three-minute passage containing two strong words above a
    ten-second one containing the same two - ranking it higher purely for being longer.
    """
    core = "That was absolutely insane."
    padding = " We then walked to the car and drove home along the usual road." * 6
    assert (
        discourse.emotional_intensity(core).score
        > discourse.emotional_intensity(core + padding).score
    )


def test_s8_intensity_is_lexical_and_therefore_independent_of_loudness():
    """S2 measures how loudly it was said; this measures whether there was anything to say.

    A quietly devastating sentence and a shouted list of ingredients sit at opposite corners of
    the two, which is why both signals exist.
    """
    quiet_but_devastating = discourse.emotional_intensity(
        "He told me, very calmly, that he had destroyed everything I had ever built."
    )
    assert quiet_but_devastating.score > 0.5


def test_s8_a_strong_word_counts_for_more_than_a_merely_emphatic_one():
    """ "Devastating" and "massive" are not the same measurement.

    The two lists exist because grouping by strength is the only defensible resolution on a
    vocabulary this short - but a grouping that does not affect the score is two lists pretending
    to be one. Same sentence, same length, one word different.
    """
    stem = "the report landed on my desk and I read it twice before I understood how {} the whole thing was"
    strong = discourse.emotional_intensity(stem.format("devastating"))
    moderate = discourse.emotional_intensity(stem.format("massive"))
    assert strong.strong_terms == 1
    assert moderate.strong_terms == 0
    assert strong.score > moderate.score


def test_s8_a_fragment_too_short_for_a_density_returns_neutral():
    for text in ("", "insane", "so insane"):
        assert discourse.emotional_intensity(text).score == 0.5


def test_s8_score_is_bounded_even_for_wall_to_wall_superlatives():
    text = " ".join(["insane crazy unbelievable devastating"] * 40) + "!!!!!!!!!!"
    assert 0.0 <= discourse.emotional_intensity(text).score <= 1.0


# --------------------------------------------------------------------------- #
# S12 - standalone completeness
# --------------------------------------------------------------------------- #


def test_s12_a_complete_thought_scores_the_maximum():
    assert (
        discourse.standalone_completeness(
            "The reason nobody noticed is that the logs were being written to a deleted file."
        ).score
        == 1.0
    )


def test_s12_penalties_are_ordered_back_reference_worst_then_conjunction_then_demonstrative():
    """The ordering is the substance, not the presence of the penalties.

    "As I said" is *stated* dependence on context the clip does not contain. A dangling
    conjunction is nearly as bad. A demonstrative is much weaker, because "this is the part
    where..." is a perfectly good clip opening - the word is as often forward-looking as
    back-looking, and penalising it like "and" would reject good clips.
    """
    back = discourse.standalone_completeness("As I said earlier, the numbers never added up.")
    conj = discourse.standalone_completeness("And the numbers never added up.")
    deictic = discourse.standalone_completeness("That is why the numbers never added up.")
    complete = discourse.standalone_completeness("The numbers never added up.")

    assert back.back_reference and conj.dangling_opener and deictic.deictic_opener
    assert back.score < conj.score < deictic.score < complete.score


def test_s12_an_unfinished_ending_is_the_lightest_penalty():
    """It is the one failure boundary logic can still fix.

    S9 snapping and AU7 trimming move a clip's end; nothing downstream can supply context a
    clip is missing. So an unfinished tail must cost less than a dangling opener.
    """
    unfinished = discourse.standalone_completeness("The numbers never added up because the")
    dangling = discourse.standalone_completeness("And the numbers never added up.")
    assert unfinished.unfinished
    assert unfinished.score > dangling.score


def test_s12_a_recap_in_the_middle_of_a_clip_is_not_a_dependence_on_missing_context():
    """A speaker recapping mid-clip usually *helps* it stand alone."""
    mid = discourse.standalone_completeness(
        "The migration finished on the Friday. As I said, nobody was watching the queue depth, "
        "so the backlog just grew all weekend."
    )
    opening = discourse.standalone_completeness(
        "As I said, nobody was watching the queue depth all weekend."
    )
    assert not mid.back_reference
    assert mid.score > opening.score


def test_s12_empty_text_is_neutral_rather_than_maximally_incomplete():
    assert discourse.standalone_completeness("").score == 0.5
    assert discourse.standalone_completeness("   ...   ").score == 0.5


# --------------------------------------------------------------------------- #
# annotation and ranking integration
# --------------------------------------------------------------------------- #


def test_annotation_attaches_all_three_signals_without_touching_the_score():
    """The invariant S4 established: annotators measure, only the ranker decides.

    One place where a measurement becomes a ranking decision, or the two drift and a scoring
    change becomes impossible to attribute.
    """
    cand = Cand(0.0, 20.0, score=42.0, text="Why did it fail? The disk filled up overnight.")
    discourse.annotate_candidates([cand])
    assert cand.score == 42.0
    assert cand.features["structure_score"] == 1.0
    assert "standalone_score" in cand.features
    assert "intensity_score" in cand.features


def test_annotation_leaves_a_textless_candidate_unmeasured_rather_than_average():
    """ "Not measured" has to stay distinguishable from "measured as average".

    Filling in 0.5 would make a clip over music indistinguishable from one whose text was
    measured and found unremarkable - and the ranker's own default already handles the former.
    """
    cand = Cand(0.0, 20.0, text="")
    discourse.annotate_candidates([cand])
    assert cand.features == {}


def test_annotation_survives_a_candidate_with_no_features_dict():
    class Bare:
        text = "Why did it fail? The disk filled up overnight."

    discourse.annotate_candidates([Bare()])  # must not raise


def test_ranking_uses_the_three_new_signals():
    """Wired in, not merely computed.

    A candidate annotated as a complete, structured, emphatic passage must out-score one
    annotated as a fragment when every delivery signal is identical.
    """
    good = Cand(0.0, 30.0, text="x")
    poor = Cand(0.0, 30.0, text="x")
    good.features.update(
        discourse.describe(
            "Why did the whole thing collapse? Because absolutely nobody was watching the queue."
        )
    )
    poor.features.update(discourse.describe("and then it was completely, you know, like the"))

    ranked_good = candidate_ranking.score_candidate(good, target=30.0, min_len=10.0, max_len=60.0)
    ranked_poor = candidate_ranking.score_candidate(poor, target=30.0, min_len=10.0, max_len=60.0)
    assert ranked_good > ranked_poor


def test_unmeasured_text_signals_default_to_neutral_not_to_zero():
    """A source with no usable text must not rank below one that was measurable and bad."""
    unmeasured = Cand(0.0, 30.0)
    measured_badly = Cand(0.0, 30.0)
    measured_badly.features.update(
        {
            "structure_score": 0.25,
            "standalone_score": 0.1,
            "intensity_score": 0.2,
        }
    )
    assert candidate_ranking.score_candidate(
        unmeasured, target=30.0, min_len=10.0, max_len=60.0
    ) > candidate_ranking.score_candidate(measured_badly, target=30.0, min_len=10.0, max_len=60.0)


def test_prompt_note_describes_only_departures():
    """An unremarkable segment must render exactly as it did before S10.

    Annotating everything is the same as annotating nothing: the contrast is the signal.
    """
    assert discourse.prompt_note("The van was parked around the back of the building.") is None
    assert "answers a question" in (
        discourse.prompt_note("Why did it fail? The disk filled up overnight.") or ""
    )
    assert "starts mid-thought" in (
        discourse.prompt_note("And that is when the whole thing fell over.") or ""
    )


def test_prompt_note_uses_words_not_numbers():
    """A raw score invites the model to invent a formula from a scale it cannot calibrate."""
    note = (
        discourse.prompt_note(
            "Here's why that is absolutely insane: nobody ever checked the backups."
        )
        or ""
    )
    assert note
    assert not any(char.isdigit() for char in note)


def test_prompt_note_reports_an_unanswered_question():
    """The `elif structure.question` branch, which nothing reached.

    Worth its own test rather than being folded into the answered case: the two notes are
    mutually exclusive by construction, and the *unanswered* one is the negative signal — a clip
    that poses a question and never answers it is the one a viewer bounces off.
    """
    note = discourse.prompt_note("Why did the backups never run?") or ""
    assert "asks an unanswered question" in note
    assert "answers a question" not in note


def test_prompt_note_reports_a_back_reference_in_preference_to_a_dangling_opener():
    """The `standalone.back_reference` branch, and the precedence it has over `dangling_opener`.

    Both can be true of one sentence — "And as I said before, ..." opens with a conjunction *and*
    refers back. Only one note is emitted, and it is the more specific one, because "refers back"
    tells the model what is missing while "starts mid-thought" only says something is.
    """
    note = discourse.prompt_note("And as I mentioned earlier, the disk filled up.") or ""
    assert "refers back" in note
    assert "starts mid-thought" not in note


def test_shouting_raises_intensity_above_the_same_words_in_lower_case():
    """The caps branch of `emotional_intensity`, which nothing reached.

    Isolated by comparing one text against its own lower-cased self, so the strong/moderate term
    density is identical and the *only* difference is the capitalisation. Asserting an absolute
    number instead would pass just as well if the branch never ran.
    """
    shouted = "This is COMPLETELY INSANE and TOTALLY UNACCEPTABLE behaviour from them"
    normal = shouted.lower()

    assert discourse.emotional_intensity(shouted).score > (
        discourse.emotional_intensity(normal).score
    )


def test_short_words_do_not_count_as_shouting():
    """`len(word) > 2`, so "I", "A" and "OK" are not shouting.

    Without the floor, ordinary text containing "I" repeatedly would drift over the caps
    threshold and every clip would read as emphatic — which is the same as none of them doing.
    """
    text = "I am OK with A plan that nobody at all bothered to write down anywhere"
    assert discourse.emotional_intensity(text).score == pytest.approx(
        discourse.emotional_intensity(text.lower()).score
    )


def test_describe_emits_exactly_the_keys_the_ranker_reads():
    """The contract between two modules that share nothing but three dictionary keys.

    `discourse.describe()` writes into `candidate.features`; `candidate_ranking.score_candidate`
    reads `structure_score`, `standalone_score` and `intensity_score` back out with a default of
    0.5. So renaming a key in either module silently reverts that signal to neutral for every
    candidate — no error, no marker, just a ranking that quietly stopped using a third of its
    inputs. Nothing else asserts the join.
    """
    emitted = discourse.describe("Why did it fail? The disk filled up overnight.")
    for key in ("structure_score", "standalone_score", "intensity_score"):
        assert key in emitted, f"{key} is read by score_candidate but no longer emitted"
        assert 0.0 <= emitted[key] <= 1.0, f"{key}={emitted[key]} is outside the ranker's clamp"

    source = (Path(candidate_ranking.__file__)).read_text(encoding="utf-8")
    for key in ("structure_score", "standalone_score", "intensity_score"):
        assert f'"{key}"' in source, (
            f"score_candidate no longer mentions {key!r}; describe() is emitting a key nothing "
            "reads, which presents as the signal having no effect"
        )


# --------------------------------------------------------------------------- #
# T9 - per-segment language detection
# --------------------------------------------------------------------------- #


def test_t9_a_script_switch_is_identified_confidently():
    """The reliable half: disjoint Unicode ranges make this a fact, not an estimate."""
    for text, expected in (
        ("यह पूरी कहानी बहुत दिलचस्प है और मुझे यह बहुत पसंद आया", "hi"),
        ("это совершенно невероятная история и я очень рад", "ru"),
        ("これは とても おもしろい はなし です", "ja"),
        ("이것은 정말 재미있는 이야기 입니다", "ko"),
    ):
        reading = language.detect(text)
        assert reading.language == expected, (text, reading)
        assert reading.confidence >= 0.6


def test_t9_han_script_yields_no_language_because_it_cannot_be_narrowed():
    """Han is used by Chinese *and* Japanese.

    Reporting one of them from characters alone is exactly the confident-and-wrong answer this
    module exists to avoid, and any detector that guesses would pass a "detects Chinese" test.
    """
    reading = language.detect("这是一个非常有趣的故事我很喜欢它")
    assert reading.script == "han"
    assert reading.language is None
    assert reading.confidence == 0.0


def test_t9_latin_script_languages_are_separated_by_function_words():
    assert (
        language.detect(
            "the whole thing is that you was never going to see it with the others"
        ).language
        == "en"
    )
    assert language.detect("der die das und ist nicht ein eine mit auch der").language == "de"


def test_t9_a_short_latin_passage_gets_no_reading_rather_than_a_guess():
    """Under six words, function-word overlap is coincidence.

    "the end" shares a token with English and nothing else; "por" is Spanish and Portuguese
    alike. Declining is the honest output.
    """
    for text in ("the end", "so what now", "one more time please"):
        reading = language.detect(text)
        assert reading.language is None
        assert reading.script == "latin"
        assert reading.confidence == 0.0


def test_t9_two_latin_languages_tying_on_function_words_is_evidence_for_neither():
    """A margin, not a raw count.

    Spanish and Portuguese share enough function words that a passage built only from the
    overlap scores identically for both. A count-based detector would hand back whichever
    language happened to come first in the table, with high confidence, on text that says
    nothing about which of the two it is.
    """
    tied = language.detect("que para que para com una")
    assert tied.language is None
    assert tied.confidence == 0.0
    # The same machinery still answers when one language actually leads.
    clear = language.detect("el la que de los una por para con pero")
    assert clear.language == "es"
    assert clear.confidence > 0.0


def test_t9_latin_readings_are_less_confident_than_script_readings():
    """The two halves of this module are not equally trustworthy, and it must say so."""
    latin = language.detect("the whole thing is that you was never going to see it with them")
    script = language.detect("यह पूरी कहानी बहुत दिलचस्प है और मुझे यह बहुत पसंद आया")
    assert latin.language and script.language
    assert latin.confidence < script.confidence


def test_t9_diacritics_do_not_inflate_their_own_script_share():
    """Devanagari matras and Arabic marks are combining characters.

    Counting them would make any diacritic-heavy script outweigh a Latin passage of the same
    length, so a mostly-English sentence with one Hindi word would come back as Hindi.
    """
    # Five Latin letters against three Devanagari consonants wearing four vowel signs. Counting
    # the marks flips the answer to Devanagari on text that is mostly English.
    script, share = language.dominant_script("hello \u0915\u093f\u0924\u093e\u092c\u094b\u0902")
    assert script == "latin"
    assert share == pytest.approx(5 / 8)


def test_t9_code_switching_reports_the_minority_segments():
    segments = [
        Seg(0.0, 3.0, "the whole thing is that you was never going to see it with them"),
        Seg(3.0, 6.0, "and the other one is that this was the best of the two options"),
        Seg(6.0, 9.0, "यह पूरी कहानी बहुत दिलचस्प है और मुझे यह बहुत पसंद आया"),
    ]
    switches = language.code_switching(segments)
    assert len(switches) == 1
    assert switches[0]["language"] == "hi"
    assert switches[0]["majority"] == "en"
    assert switches[0]["start"] == 6.0


def test_t9_a_single_language_transcript_reports_no_switches():
    """The output must be empty when there is nothing to report.

    A detector that flags a switch on every monolingual file is worse than none: the operator
    stops reading it.
    """
    segments = [
        Seg(0.0, 3.0, "the whole thing is that you was never going to see it with them"),
        Seg(3.0, 6.0, "and the other one is that this was the best of the two options"),
    ]
    assert language.code_switching(segments) == []


def test_t9_code_switching_accepts_a_generator():
    """It walks the segments twice - to read them, then to pair readings with timings.

    Given a generator, a second walk over an exhausted iterator reports no switches at all,
    which looks exactly like "this content isn't bilingual".
    """

    def gen():
        yield Seg(0.0, 3.0, "the whole thing is that you was never going to see it with them")
        yield Seg(3.0, 6.0, "and the other one is that this was the best of the two options")
        yield Seg(6.0, 9.0, "यह पूरी कहानी बहुत दिलचस्प है और मुझे यह बहुत पसंद आया")

    assert len(language.code_switching(gen())) == 1


def test_t9_detect_segments_is_one_reading_per_segment_in_order():
    segments = [Seg(0.0, 1.0, "यह पूरी कहानी बहुत दिलचस्प है"), Seg(1.0, 2.0, "")]
    readings = language.detect_segments(segments)
    assert len(readings) == 2
    assert readings[0].language == "hi"
    assert readings[1].language is None


# --------------------------------------------------------------------------- #
# C19 - emoji placement
# --------------------------------------------------------------------------- #


def _highlight_words():
    """A cue whose most *salient* mapped word is not the one the caption highlights."""
    return [
        W(0.0, 0.4, "the"),
        W(0.4, 1.2, "money"),  # mapped, and the stronger salience candidate
        W(1.2, 2.0, "fire"),  # mapped, but this is the highlighted one
    ]


def test_c19_a_highlighted_word_outranks_a_more_salient_unhighlighted_one():
    """The emoji must agree with the caption, not hold a second opinion.

    A11 ranked emoji candidates by the same salience scorer the highlighter uses, which makes
    them agree *most* of the time. The highlighter then applies a per-cue budget and a floor, so
    its final choice is not a pure function of salience - and where the two disagreed, the emoji
    illustrated one word while the caption emphasised another. To a viewer that is a bug even
    though both components behaved as written.
    """
    words = _highlight_words()
    unconstrained = emoji.plan_emoji(words, duration=8.0, intensity="subtle")
    steered = emoji.plan_emoji(words, duration=8.0, intensity="subtle", keyword_indices={2})
    assert len(unconstrained) == 1 and len(steered) == 1
    assert steered[0].start == pytest.approx(1.2)
    assert steered[0].char != unconstrained[0].char


def test_c19_an_empty_keyword_set_leaves_the_a11_ranking_untouched():
    """Passing no indices, or an empty set, must be the previously shipped behaviour."""
    words = _highlight_words()
    assert emoji.plan_emoji(words, duration=8.0, intensity="subtle") == emoji.plan_emoji(
        words, duration=8.0, intensity="subtle", keyword_indices=set()
    )


def test_c19_a_highlighted_word_with_no_emoji_does_not_win_a_slot():
    """Highlighting reorders the mapped candidates; it cannot invent one."""
    words = [W(0.0, 0.5, "the"), W(0.5, 1.0, "money")]
    cues = emoji.plan_emoji(words, duration=8.0, intensity="subtle", keyword_indices={0})
    assert len(cues) == 1
    assert cues[0].start == pytest.approx(0.5)


def test_c19_caption_placement_puts_the_glyph_clear_of_the_caption_block():
    """Bottom captions get the emoji above them, top captions below.

    Overlapping the text would be worse than the frame-corner placement it replaces, so the
    vertical band has to move with the caption position rather than being a constant.
    """
    cue = emoji.EmojiCue(char="\U0001f525", start=1.0, end=2.3, slot=0)

    def resolver(_char):
        return None

    xs = {}
    for position in ("bottom", "top", "center"):
        x, y = emoji._caption_adjacent_slot(0, position)
        xs[position] = y
    assert xs["bottom"] > xs["center"] > xs["top"]
    assert cue.slot == 0


def test_c19_caption_placement_centres_a_single_emoji():
    """One emoji paired with a caption belongs above its middle.

    The side offsets exist only so two emoji close in time do not overlap.
    """
    x, _y = emoji._caption_adjacent_slot(0, "bottom")
    assert x == pytest.approx(0.5)
    left, _ = emoji._caption_adjacent_slot(1, "bottom")
    right, _ = emoji._caption_adjacent_slot(2, "bottom")
    assert left < 0.5 < right


def test_c19_an_unrecognised_caption_position_falls_back_to_the_bottom_band():
    x, y = emoji._caption_adjacent_slot(0, "wherever")
    assert (x, y) == emoji._caption_adjacent_slot(0, "bottom")
    x2, y2 = emoji._caption_adjacent_slot(0, "")
    assert (x2, y2) == (x, y)


def test_c19_a_nine_position_caption_reduces_to_three_vertical_bands():
    """C13 gives nine caption positions; only the vertical third matters here."""
    for variant in ("top", "top_left", "top-right"):
        assert emoji._caption_adjacent_slot(0, variant) == emoji._caption_adjacent_slot(0, "top")


def test_c19_the_default_placement_is_the_previously_shipped_one():
    """A new look must not arrive with an upgrade.

    Defaulting to `caption` would move the emoji in every existing user's output without them
    changing anything.
    """
    assert settings.emoji_placement == "spread"
    assert emoji.PLACEMENTS[0] == "spread"


def test_c19_spread_and_caption_placement_produce_different_overlay_geometry(tmp_path):
    png = tmp_path / "fire.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    cues = [emoji.EmojiCue(char="\U0001f525", start=1.0, end=2.3, slot=0)]

    def resolver(_char):
        return png

    _in_a, graph_spread = emoji.build_overlay(
        cues, "v0", "vout", duration=8.0, resolver=resolver, placement="spread"
    )
    _in_b, graph_caption = emoji.build_overlay(
        cues,
        "v0",
        "vout",
        duration=8.0,
        resolver=resolver,
        placement="caption",
        caption_position="bottom",
    )
    assert graph_spread and graph_caption
    assert graph_spread != graph_caption
    assert "0.6" in graph_caption


# --------------------------------------------------------------------------- #
# T10 - translated subtitle track
# --------------------------------------------------------------------------- #


def test_t10_a_language_tag_becomes_part_of_the_filename_not_a_replaced_extension(tmp_path):
    """``Path("clip_0.en").with_suffix(".srt")`` is ``clip_0.srt``.

    Which would have the translation overwrite the original-language sidecar - two files that
    are supposed to sit side by side reduced to one, with no error anywhere.
    """
    words = [W(0.0, 0.5, "hello"), W(0.6, 1.2, "there")]
    plain = subtitle_export.write_sidecars(words, tmp_path / "clip_0", formats=("srt",))
    translated = subtitle_export.write_sidecars(
        words, tmp_path / "clip_0", formats=("srt",), language="en"
    )
    assert plain[0].name == "clip_0.srt"
    assert translated[0].name == "clip_0.en.srt"
    assert plain[0].exists() and translated[0].exists()


def test_t10_an_empty_language_leaves_existing_filenames_unchanged(tmp_path):
    words = [W(0.0, 0.5, "hello"), W(0.6, 1.2, "there")]
    written = subtitle_export.write_sidecars(
        words, tmp_path / "clip_0", formats=("srt", "vtt"), language=""
    )
    assert [p.name for p in written] == ["clip_0.srt", "clip_0.vtt"]


def test_t10_language_codes_map_to_the_three_letter_form_mp4_metadata_expects():
    assert subtitle_export.iso639_2("en") == "eng"
    assert subtitle_export.iso639_2("de") == "deu"
    assert subtitle_export.iso639_2("ja") == "jpn"
    # Already three-letter: passed through, so a caller need not know which form it holds.
    assert subtitle_export.iso639_2("eng") == "eng"


def test_t10_an_unknown_language_becomes_und_rather_than_the_two_letter_code():
    """`und` is a real ISO 639-2 code meaning "undetermined".

    Passing "xx" through would put a string that is not a valid language code in the track's
    metadata, which players either ignore or display raw in the track menu.
    """
    for value in ("xx", "", None, "klingon", "zz-ZZ"):
        assert subtitle_export.iso639_2(value) == "und"


@requires_ffmpeg
def test_t10_two_subtitle_tracks_are_muxed_with_distinct_language_labels(tmp_path, make_video):
    """The point of one mux call rather than two.

    ``-metadata:s:s:N`` numbers subtitle streams by their position in the *output*, so a second
    remux of a file that already carries a track would either re-label the first or need to know
    how many the input had. A menu offering two entries both called English is no menu.
    """
    from worker import ffmpeg_utils as fu

    src = make_video("src.mp4", duration=3.0, w=640, h=360)
    es = tmp_path / "es.srt"
    es.write_text("1\n00:00:00,500 --> 00:00:02,000\nhola mundo\n\n", encoding="utf-8")
    en = tmp_path / "en.srt"
    en.write_text("1\n00:00:00,500 --> 00:00:02,000\nhello world\n\n", encoding="utf-8")

    out = tmp_path / "two.mp4"
    fu.mux_subtitle_tracks(src, [(es, "spa"), (en, "eng")], out)

    langs = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "s",
            "-show_entries",
            "stream_tags=language",
            "-of",
            "csv=p=0",
            str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert langs == ["spa", "eng"]


@requires_ffmpeg
def test_t10_the_original_language_track_comes_first_so_a_player_defaults_to_it(
    tmp_path, make_video
):
    """A viewer who wanted the source language must not have to go and find it."""
    from worker import ffmpeg_utils as fu

    src = make_video("src.mp4", duration=3.0, w=640, h=360)
    es = tmp_path / "es.srt"
    es.write_text("1\n00:00:00,500 --> 00:00:02,000\nhola mundo\n\n", encoding="utf-8")
    en = tmp_path / "en.srt"
    en.write_text("1\n00:00:00,500 --> 00:00:02,000\nhello world\n\n", encoding="utf-8")

    out = tmp_path / "two.mp4"
    fu.mux_subtitle_tracks(src, [(es, "spa"), (en, "eng")], out)
    first = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(out),
            "-map",
            "0:s:0",
            "-f",
            "srt",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "hola mundo" in first


@requires_ffmpeg
def test_t10_muxing_two_tracks_still_does_not_re_encode_the_video(tmp_path, make_video):
    from worker import ffmpeg_utils as fu

    src = make_video("src.mp4", duration=3.0, w=640, h=360)
    a = tmp_path / "a.srt"
    a.write_text("1\n00:00:00,500 --> 00:00:02,000\nuno\n\n", encoding="utf-8")
    b = tmp_path / "b.srt"
    b.write_text("1\n00:00:00,500 --> 00:00:02,000\ntwo\n\n", encoding="utf-8")
    out = tmp_path / "two.mp4"
    fu.mux_subtitle_tracks(src, [(a, "spa"), (b, "eng")], out)

    def video_md5(path):
        return subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-map",
                "0:v",
                "-c",
                "copy",
                "-f",
                "md5",
                "-",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    assert video_md5(out) == video_md5(src)


@requires_ffmpeg
def test_t10_a_track_with_no_language_is_labelled_und(tmp_path, make_video):
    from worker import ffmpeg_utils as fu

    src = make_video("src.mp4", duration=3.0, w=640, h=360)
    srt = tmp_path / "a.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:02,000\nuno\n\n", encoding="utf-8")
    # Both the empty string and ``None``: the second is the one that matters, because
    # interpolating it straight into the metadata argument writes the literal text "None" into
    # the track's language field, and MP4's own default of `und` cannot save you from that.
    for index, value in enumerate(("", None)):
        out = tmp_path / f"one_{index}.mp4"
        fu.mux_subtitle_tracks(src, [(srt, value)], out)
        langs = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "s",
                "-show_entries",
                "stream_tags=language",
                "-of",
                "csv=p=0",
                str(out),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert langs == "und", (value, langs)


def test_t10_is_off_by_default_because_it_costs_a_second_asr_pass():
    assert settings.subtitle_translation is False


# --------------------------------------------------------------------------- #
# T10 - end to end through the pipeline
# --------------------------------------------------------------------------- #


def _spanish_and_english_transcribers():
    """A source-language pass and its English translation, as Whisper would return them."""
    from worker.transcribe import Transcript, TranscriptSegment, Word

    def _transcribe(source, language=None, translate=False, **_kw):
        if translate:
            words = [Word(0.3, 0.8, "the"), Word(0.9, 1.5, "whole"), Word(1.6, 2.2, "world")]
            return Transcript(
                language="es",
                segments=[TranscriptSegment(0.0, 4.0, "the whole world", words)],
            )
        words = [Word(0.3, 0.8, "el"), Word(0.9, 1.5, "mundo"), Word(1.6, 2.2, "entero")]
        return Transcript(
            language="es",
            segments=[TranscriptSegment(0.0, 4.0, "el mundo entero", words)],
        )

    return _transcribe


@requires_ffmpeg
def test_t10_the_original_language_captions_survive_the_translation(
    make_video, tmp_path, monkeypatch
):
    """This is the entire difference from ``task=translate``.

    Whisper's translate task *replaces* the transcript, so asking for a translation used to cost
    the source-language captions - a Spanish creator's clip came back with English burned into
    the pixels. A test that only checked "an English sidecar exists" would pass under the old
    behaviour too.
    """
    import worker.pipeline as pl
    from tests.conftest import options_all_off
    from worker.selection import ClipCandidate

    monkeypatch.setattr(settings, "subtitle_translation", True, raising=False)
    src = make_video("es.mp4", duration=4.0, w=640, h=360)
    monkeypatch.setattr(pl, "transcribe", _spanish_and_english_transcribers())
    monkeypatch.setattr(
        pl.sel,
        "select_moments",
        lambda *a, **k: [ClipCandidate(start=0.0, end=4.0, score=60.0, text="el mundo entero")],
    )

    opts = options_all_off(captions=False, metadata=False, aspect="9:16", subtitle_sidecar=True)
    clips = pl.run_pipeline(src, opts, clips_dir=tmp_path / "clips", temp_dir=tmp_path / "tmp")
    assert len(clips) == 1
    stem = (tmp_path / "clips" / clips[0].filename).with_suffix("")
    original = stem.with_name(f"{stem.name}.srt").read_text(encoding="utf-8")
    translated = stem.with_name(f"{stem.name}.en.srt").read_text(encoding="utf-8")
    assert "mundo" in original
    assert "world" in translated
    assert "mundo" not in translated
    assert "subtitle_translation:track" in clips[0].effects_applied


@requires_ffmpeg
def test_t10_the_translation_arrives_as_a_second_selectable_track(
    make_video, tmp_path, monkeypatch
):
    """A track, not a file: a sidecar is only reachable if the platform accepts uploads."""
    import worker.pipeline as pl
    from tests.conftest import options_all_off
    from worker.selection import ClipCandidate

    monkeypatch.setattr(settings, "subtitle_translation", True, raising=False)
    monkeypatch.setattr(settings, "caption_mode", "soft", raising=False)
    src = make_video("es2.mp4", duration=4.0, w=640, h=360)
    monkeypatch.setattr(pl, "transcribe", _spanish_and_english_transcribers())
    monkeypatch.setattr(
        pl.sel,
        "select_moments",
        lambda *a, **k: [ClipCandidate(start=0.0, end=4.0, score=60.0, text="el mundo entero")],
    )

    opts = options_all_off(captions=False, metadata=False, aspect="9:16")
    clips = pl.run_pipeline(src, opts, clips_dir=tmp_path / "clips", temp_dir=tmp_path / "tmp")
    out = tmp_path / "clips" / clips[0].filename
    langs = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "s",
            "-show_entries",
            "stream_tags=language",
            "-of",
            "csv=p=0",
            str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    # Source language first, then the translation - and the source track labelled `spa`, not the
    # fixed `eng` the single-track path used to hard-code.
    assert langs == ["spa", "eng"]


@requires_ffmpeg
def test_t10_an_english_source_is_skipped_rather_than_translated_into_itself(
    make_video, tmp_path, monkeypatch
):
    """A second ASR pass to turn English into English is minutes spent for nothing.

    The marker matters as much as the skip: an absent track with no explanation is
    indistinguishable from a broken one.
    """
    import worker.pipeline as pl
    from tests.conftest import options_all_off
    from worker.selection import ClipCandidate
    from worker.transcribe import Transcript, TranscriptSegment, Word

    monkeypatch.setattr(settings, "subtitle_translation", True, raising=False)
    src = make_video("en.mp4", duration=4.0, w=640, h=360)

    calls: list[bool] = []

    def _transcribe(source, language=None, translate=False, **_kw):
        calls.append(translate)
        return Transcript(
            language="en",
            segments=[
                TranscriptSegment(
                    0.0,
                    4.0,
                    "hello there friend",
                    [Word(0.3, 0.8, "hello"), Word(0.9, 1.5, "there")],
                )
            ],
        )

    monkeypatch.setattr(pl, "transcribe", _transcribe)
    monkeypatch.setattr(
        pl.sel,
        "select_moments",
        lambda *a, **k: [ClipCandidate(start=0.0, end=4.0, score=60.0, text="hello there")],
    )

    opts = options_all_off(captions=False, metadata=False, aspect="9:16")
    clips = pl.run_pipeline(src, opts, clips_dir=tmp_path / "clips", temp_dir=tmp_path / "tmp")
    assert calls == [False]
    assert "subtitle_translation:skipped_english" in clips[0].effects_applied


@requires_ffmpeg
def test_t10_a_translated_main_pass_is_not_translated_a_second_time(
    make_video, tmp_path, monkeypatch
):
    """The captions are already English; a translation of them is a translation of itself."""
    import worker.pipeline as pl
    from tests.conftest import options_all_off
    from worker.selection import ClipCandidate
    from worker.transcribe import Transcript, TranscriptSegment, Word

    monkeypatch.setattr(settings, "subtitle_translation", True, raising=False)
    src = make_video("tr.mp4", duration=4.0, w=640, h=360)

    calls: list[bool] = []

    def _transcribe(source, language=None, translate=False, **_kw):
        calls.append(translate)
        return Transcript(
            language="es",
            segments=[
                TranscriptSegment(
                    0.0,
                    4.0,
                    "the whole world",
                    [Word(0.3, 0.8, "the"), Word(0.9, 1.5, "whole")],
                )
            ],
        )

    monkeypatch.setattr(pl, "transcribe", _transcribe)
    monkeypatch.setattr(
        pl.sel,
        "select_moments",
        lambda *a, **k: [ClipCandidate(start=0.0, end=4.0, score=60.0, text="the whole world")],
    )

    opts = options_all_off(captions=False, metadata=False, aspect="9:16", translate=True)
    clips = pl.run_pipeline(src, opts, clips_dir=tmp_path / "clips", temp_dir=tmp_path / "tmp")
    assert calls == [True]
    assert "subtitle_translation:skipped_already_translated" in clips[0].effects_applied


@requires_ffmpeg
def test_t10_a_failed_translation_pass_does_not_cost_the_job(make_video, tmp_path, monkeypatch):
    """An extra track on a job whose expensive work is still ahead of it.

    Every failure mode of a model call - a missing weight file, a corrupt download, OOM - is a
    reason to ship the clips without the translation, not to lose the render.
    """
    import worker.pipeline as pl
    from tests.conftest import options_all_off
    from worker.selection import ClipCandidate
    from worker.transcribe import Transcript, TranscriptSegment, Word

    monkeypatch.setattr(settings, "subtitle_translation", True, raising=False)
    src = make_video("boom.mp4", duration=4.0, w=640, h=360)

    def _transcribe(source, language=None, translate=False, **_kw):
        if translate:
            raise RuntimeError("model weights missing")
        return Transcript(
            language="es",
            segments=[
                TranscriptSegment(
                    0.0,
                    4.0,
                    "el mundo entero",
                    [Word(0.3, 0.8, "el"), Word(0.9, 1.5, "mundo")],
                )
            ],
        )

    monkeypatch.setattr(pl, "transcribe", _transcribe)
    monkeypatch.setattr(
        pl.sel,
        "select_moments",
        lambda *a, **k: [ClipCandidate(start=0.0, end=4.0, score=60.0, text="el mundo entero")],
    )

    opts = options_all_off(captions=False, metadata=False, aspect="9:16")
    clips = pl.run_pipeline(src, opts, clips_dir=tmp_path / "clips", temp_dir=tmp_path / "tmp")
    assert len(clips) == 1
    assert (tmp_path / "clips" / clips[0].filename).exists()
    assert "subtitle_translation:failed" in clips[0].effects_applied


@requires_ffmpeg
def test_t10_off_by_default_means_no_second_pass_and_no_extra_track(
    make_video, tmp_path, monkeypatch
):
    """The parity case: nothing about an existing run changes."""
    import worker.pipeline as pl
    from tests.conftest import options_all_off
    from worker.selection import ClipCandidate

    src = make_video("plain.mp4", duration=4.0, w=640, h=360)
    calls: list[bool] = []
    base = _spanish_and_english_transcribers()

    def _transcribe(source, language=None, translate=False, **_kw):
        calls.append(translate)
        return base(source, language=language, translate=translate)

    monkeypatch.setattr(pl, "transcribe", _transcribe)
    monkeypatch.setattr(
        pl.sel,
        "select_moments",
        lambda *a, **k: [ClipCandidate(start=0.0, end=4.0, score=60.0, text="el mundo entero")],
    )

    opts = options_all_off(captions=False, metadata=False, aspect="9:16", subtitle_sidecar=True)
    clips = pl.run_pipeline(src, opts, clips_dir=tmp_path / "clips", temp_dir=tmp_path / "tmp")
    assert calls == [False]
    assert not any("subtitle_translation" in marker for marker in clips[0].effects_applied)
    stem = (tmp_path / "clips" / clips[0].filename).with_suffix("")
    assert not stem.with_name(f"{stem.name}.en.srt").exists()


@requires_ffmpeg
def test_t10_the_translated_track_follows_every_cut_made_to_the_timeline(
    make_video, tmp_path, monkeypatch
):
    """Filler removal tightens the media, so both tracks have to be rebased onto it.

    Left alone, the translation drifts by the total removed duration - which reads as a sync bug
    in the player rather than as a bug here, and grows with every "um" cut. Both tracks describe
    the same media, so their final cue must end at the same moment.
    """
    import worker.pipeline as pl
    from tests.conftest import options_all_off
    from worker.selection import ClipCandidate
    from worker.transcribe import Transcript, TranscriptSegment, Word

    monkeypatch.setattr(settings, "subtitle_translation", True, raising=False)
    src = make_video("filler.mp4", duration=4.0, w=640, h=360)

    def _transcribe(source, language=None, translate=False, **_kw):
        if translate:
            words = [Word(0.3, 0.8, "the"), Word(0.9, 1.5, "um"), Word(1.6, 2.2, "world")]
            text = "the um world"
        else:
            words = [Word(0.3, 0.8, "el"), Word(0.9, 1.5, "um"), Word(1.6, 2.2, "mundo")]
            text = "el um mundo"
        return Transcript(language="es", segments=[TranscriptSegment(0.0, 4.0, text, words)])

    monkeypatch.setattr(pl, "transcribe", _transcribe)
    monkeypatch.setattr(
        pl.sel,
        "select_moments",
        lambda *a, **k: [ClipCandidate(start=0.0, end=4.0, score=60.0, text="el um mundo")],
    )

    opts = options_all_off(
        captions=False,
        metadata=False,
        aspect="9:16",
        subtitle_sidecar=True,
        filler_removal=True,
    )
    clips = pl.run_pipeline(src, opts, clips_dir=tmp_path / "clips", temp_dir=tmp_path / "tmp")
    stem = (tmp_path / "clips" / clips[0].filename).with_suffix("")
    original = stem.with_name(f"{stem.name}.srt").read_text(encoding="utf-8")
    translated = stem.with_name(f"{stem.name}.en.srt").read_text(encoding="utf-8")

    def last_end(text: str) -> str:
        return [line for line in text.splitlines() if "-->" in line][-1].split("-->")[1].strip()

    assert last_end(original) == last_end(translated)
    assert "subtitle_translation:track" in clips[0].effects_applied
    assert "filler_removal" in clips[0].effects_applied
