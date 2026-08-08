"""Pairwise human preference (M12): the only instrument for things no metric measures.

Framing, pacing and grade have no metric. SSIM cannot tell you whether a crop is well composed —
it would score a badly framed reference reproduced perfectly as 1.0. So the only way to judge those
changes is to show two renders to a person and ask which they prefer, and the only way *that*
produces information rather than confirmation is to control how the question is asked.

Four controls, each guarding against a specific way this goes wrong:

**Blind** (R5.2). Knowing which render is "the new one" produces the expected answer. The judge sees
`A` and `B`; the mapping lives in a separate answer key.

**Order randomised per trial** (R5.3). Fixed order produces position bias — with A always the
baseline, a judge who mildly prefers the left-hand video reads as preferring the baseline. The
randomisation must be per trial and must not be a function of the configuration, or it is not
randomisation at all.

**One dimension per set** (R5.7). Judging an accumulation of five changes tells you nothing about
any of them.

**Declines are data** (R5.4). "No visible difference" is the *most* useful outcome for a change
that costs render time, and discarding declines manufactures a preference from nothing.

And the honest limit, stated in the report itself rather than left to the reader: **a trial count
this small cannot distinguish a real preference from noise.** A 4-2 split is noise. This module
computes no p-value and makes no significance claim, because with a handful of trials and usually
one judge — who is usually the person who wrote the change — any such claim would be false
precision dressed as rigour.
"""

from __future__ import annotations

import json
import random
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

#: The three answers a trial can have. `DECLINE` is a first-class outcome, not a missing value.
CHOICE_A = "a"
CHOICE_B = "b"
DECLINE = "no_difference"
CHOICES: tuple[str, ...] = (CHOICE_A, CHOICE_B, DECLINE)

#: Below this many trials the report says so explicitly. Not a significance threshold -- there is
#: deliberately no such thing here -- but the point past which a reader might otherwise start
#: treating a split as a finding.
SMALL_TRIAL_COUNT = 20


@dataclass(frozen=True)
class Trial:
    """One blind comparison.

    ``slot_a``/``slot_b`` are the *presented* order. ``config_a``/``config_b`` name which
    configuration landed in which slot, and that is the answer key -- kept in the manifest and out
    of whatever the judge looks at.
    """

    trial_id: str
    slot_a: str
    slot_b: str
    config_a: str
    config_b: str
    dimension: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def presented(self) -> dict:
        """What the judge is allowed to see: two paths and nothing that identifies them."""
        return {"trial_id": self.trial_id, "a": self.slot_a, "b": self.slot_b}


@dataclass(frozen=True)
class Judgement:
    """One recorded answer."""

    trial_id: str
    choice: str
    judge: str = ""
    authored_the_change: bool = False

    def __post_init__(self) -> None:
        if self.choice not in CHOICES:
            raise ValueError(f"choice must be one of {CHOICES}, got {self.choice!r}")


@dataclass(frozen=True)
class Preference_Set:
    """A dimension, its trials, and the answer key."""

    dimension: str
    trials: tuple[Trial, ...] = ()
    baseline: str = ""
    candidate: str = ""
    seed: int | None = None

    def to_dict(self) -> dict:
        return {
            "dimension": self.dimension,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "seed": self.seed,
            "trials": [t.to_dict() for t in self.trials],
        }

    def presented(self) -> dict:
        """The blind view: trials with no configuration names anywhere in it."""
        return {
            "dimension": self.dimension,
            "trials": [t.presented() for t in self.trials],
        }


def build_set(
    dimension: str,
    pairs: Sequence[tuple[str, str]],
    *,
    baseline: str = "baseline",
    candidate: str = "candidate",
    seed: int | None = None,
) -> Preference_Set:
    """Build a blind, order-randomised trial set from ``(baseline_path, candidate_path)`` pairs.

    ``seed`` makes a set reproducible, which matters for testing the randomisation and for
    regenerating a set someone has already judged. It does **not** make the order a function of
    the configuration: within a run, which slot each configuration lands in is drawn per trial.

    One dimension per set (R5.7). The name is carried on the set and on every trial so a results
    file cannot be read without knowing what was being judged.
    """
    if not dimension.strip():
        raise ValueError("a preference set must name the single dimension it varies (R5.7)")
    # S311 is suppressed rather than satisfied: this decides which side of a screen a video
    # appears on, which is not a secret. A *seeded* generator is a requirement here rather than a
    # compromise -- a set someone has already judged has to be regenerable, and `secrets` cannot
    # do that by design.
    rng = random.Random(seed)  # noqa: S311
    trials: list[Trial] = []
    for index, (base_path, cand_path) in enumerate(pairs):
        # Per trial, not per set. A set-level coin flip would put every baseline in the same slot
        # and reintroduce exactly the position bias the randomisation exists to remove.
        baseline_first = rng.random() < 0.5
        if baseline_first:
            slot_a, slot_b = str(base_path), str(cand_path)
            config_a, config_b = baseline, candidate
        else:
            slot_a, slot_b = str(cand_path), str(base_path)
            config_a, config_b = candidate, baseline
        trials.append(
            Trial(
                trial_id=f"{dimension}-{index:03d}",
                slot_a=slot_a,
                slot_b=slot_b,
                config_a=config_a,
                config_b=config_b,
                dimension=dimension,
            )
        )
    return Preference_Set(
        dimension=dimension,
        trials=tuple(trials),
        baseline=baseline,
        candidate=candidate,
        seed=seed,
    )


def tally(preference_set: Preference_Set, judgements: Sequence[Judgement]) -> dict:
    """Count the outcomes per configuration, resolving each judgement through the answer key.

    Declines are counted, never dropped (R5.4). The count of distinct judges and whether any of
    them authored the change are reported (R5.9) — not to disqualify them, since here it is
    usually unavoidable, but so a reader can discount it.
    """
    by_id = {t.trial_id: t for t in preference_set.trials}
    counts: dict[str, int] = {preference_set.baseline: 0, preference_set.candidate: 0}
    declines = 0
    unknown: list[str] = []
    judges: set[str] = set()
    authored = False

    for judgement in judgements:
        trial = by_id.get(judgement.trial_id)
        if trial is None:
            unknown.append(judgement.trial_id)
            continue
        judges.add(judgement.judge or "anonymous")
        authored = authored or judgement.authored_the_change
        if judgement.choice == DECLINE:
            declines += 1
            continue
        chosen = trial.config_a if judgement.choice == CHOICE_A else trial.config_b
        counts[chosen] = counts.get(chosen, 0) + 1

    total = sum(counts.values()) + declines
    return {
        "dimension": preference_set.dimension,
        "trials_available": len(preference_set.trials),
        "trials_judged": total,
        "counts": counts,
        # R5.4: named `no_difference` rather than folded into a residual, because for a change
        # that costs render time this is the most useful outcome and it should be as visible as
        # a preference.
        "no_difference": declines,
        "distinct_judges": len(judges),
        "judge_authored_the_change": authored,
        "unknown_trial_ids": unknown,
        "interpretation": _interpretation(total, counts, declines),
    }


def _interpretation(total: int, counts: dict[str, int], declines: int) -> str:
    """Prose that states what the numbers cannot support (R5.5, R5.6).

    Written here rather than left to whoever reads the JSON, because the failure mode is a split
    being quoted as a result. No p-value is computed and none should be: with a handful of trials
    and usually a single judge, a significance claim would be false precision.
    """
    if total == 0:
        return (
            "No trials judged. Nothing can be concluded, and the absence of judgements is not "
            "evidence that the two renders are equivalent."
        )
    parts = [
        f"{total} trial(s) judged."
        " This is a directional signal, not a measurement:"
        " a trial count this small cannot distinguish a real preference from noise,"
        " and no significance test is applied or implied."
    ]
    if total < SMALL_TRIAL_COUNT:
        parts.append(
            f" Fewer than {SMALL_TRIAL_COUNT} trials: a split such as 4-2 is noise and must not"
            " be reported as a preference."
        )
    if declines:
        parts.append(
            f" {declines} judgement(s) reported no visible difference, which for a change that"
            " costs render time is a substantive finding rather than a missing answer."
        )
    if counts and max(counts.values()) == min(counts.values()):
        parts.append(" The split is even.")
    return "".join(parts)


#: A deliberately plain local page. No framework, no network, no hosted service (R5.8, R6.6):
#: a preference run is a directory of clip pairs, and shipping it to a third party would make it a
#: dependency and a privacy question for no benefit.
_PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Pairwise preference - {dimension}</title>
<style>
 body{{font:16px/1.5 system-ui,sans-serif;margin:2rem;max-width:60rem}}
 .pair{{display:flex;gap:1rem;margin:1rem 0}}
 video{{width:100%;background:#000}}
 .row{{border-top:1px solid #ccc;padding:1rem 0}}
 button{{font:inherit;padding:.4rem .8rem;margin-right:.5rem}}
 code{{background:#f4f4f4;padding:.1rem .3rem}}
</style>
<h1>Pairwise preference</h1>
<p>Dimension under test: <strong>{dimension}</strong></p>
<p>
  Which do you prefer? <strong>&ldquo;No visible difference&rdquo; is a real answer</strong> and is
  the most useful one if this change costs render time. Do not guess.
</p>
<p>
  You are not told which is which, and the left/right order is randomised per trial.
  Record your answers and paste them into a results JSON file.
</p>
<div id="trials"></div>
<h2>Your answers</h2>
<pre id="out">[]</pre>
<script>
const TRIALS = {trials_json};
const answers = {{}};
const host = document.getElementById("trials");
const out = document.getElementById("out");
function render() {{
  out.textContent = JSON.stringify(
    Object.entries(answers).map(([trial_id, choice]) => ({{trial_id, choice}})), null, 2);
}}
for (const t of TRIALS) {{
  const row = document.createElement("div");
  row.className = "row";
  row.innerHTML = `<p><code>${{t.trial_id}}</code></p>
    <div class="pair">
      <div><p><strong>A</strong></p><video src="${{t.a}}" controls loop></video></div>
      <div><p><strong>B</strong></p><video src="${{t.b}}" controls loop></video></div>
    </div>`;
  for (const [label, value] of [["Prefer A","a"],["Prefer B","b"],
                                ["No visible difference","no_difference"]]) {{
    const b = document.createElement("button");
    b.textContent = label;
    b.onclick = () => {{ answers[t.trial_id] = value; render(); }};
    row.appendChild(b);
  }}
  host.appendChild(row);
}}
render();
</script>
"""


def write_session(preference_set: Preference_Set, directory: str | Path) -> dict[str, Path]:
    """Write the blind page, the blind trial list, and the answer key.

    Three files, and the split is the whole point: ``index.html`` and ``trials.json`` contain **no
    configuration names**, so a judge cannot learn which render is which by reading them. The key
    is written alongside for whoever tallies the results afterwards.
    """
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)

    presented = preference_set.presented()
    trials_path = root / "trials.json"
    trials_path.write_text(json.dumps(presented, indent=2), encoding="utf-8")

    page_path = root / "index.html"
    page_path.write_text(
        _PAGE.format(
            dimension=preference_set.dimension,
            trials_json=json.dumps(presented["trials"]),
        ),
        encoding="utf-8",
    )

    key_path = root / "answer_key.json"
    key_path.write_text(json.dumps(preference_set.to_dict(), indent=2), encoding="utf-8")

    return {"page": page_path, "trials": trials_path, "key": key_path}
