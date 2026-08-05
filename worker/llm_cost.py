"""Per-job LLM token accounting (Phase 7).

Every job makes LLM calls - selection, metadata, keyword planning, emoji, regeneration - and
none of them were counted. For a self-hosted tool whose main running cost *is* tokens, that is
a conspicuous gap: the operator paid a bill they could not attribute to anything, and "which
job cost that" was unanswerable.

The mechanism is the M5 stage-timing one applied to a different quantity: record on the worker
thread, attribute through :mod:`worker.observability`'s job context, aggregate onto the job
record so it survives a restart, and surface it beside the timings the UI already shows.

**Tokens are a fact; money is a judgement.** These are kept apart deliberately, and it is the
one design decision here worth defending. Token counts come from the provider and are simply
true. A *price* does not: it depends on the model, the account, the tier, and the date, and it
changes without notice. So this module always counts tokens, and reports ``cost_usd`` only when
a rate has been configured - otherwise ``None``.

``None`` rather than ``0.0``, because "this cost nothing" and "nobody told me what this costs"
are different facts and the second one must not be reported as the first. An unpriced job
showing ``$0.00`` is the confidently-wrong answer, and it would be *acted on* - someone would
conclude their token spend was negligible. The same reasoning as ``language.py`` declining to
name a language for Han script rather than guessing between Chinese and Japanese.

**There is deliberately no built-in price table.** Shipping one would make this work out of the
box, and it would be wrong within a quarter - silently, because a stale rate still produces a
plausible number. A price table in a repository nobody updates is a slow-motion version of the
defect this whole programme exists to remove. The rates are two settings; an operator who wants
costing sets them for the model they actually use, and one who does not gets token counts and
no false precision.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from config import settings
from worker import observability

logger = logging.getLogger(__name__)

#: Provider field names for the same two numbers. OpenAI says ``prompt``/``completion``,
#: Anthropic says ``input``/``output``, and both are spelled on an object called ``usage`` - so a
#: reader of either SDK alone would reasonably assume their own spelling is *the* spelling.
#:
#: This is the resolver hazard the working agreement names: the value that matters is what came
#: *out*, not what a field is called at one call site. Reading only OpenAI's names would leave
#: Anthropic silently unmetered - every call recorded, every token count zero, and a cost report
#: that looks complete. Hence both spellings here and a test per provider shape.
_PROMPT_FIELDS = ("prompt_tokens", "input_tokens")
_COMPLETION_FIELDS = ("completion_tokens", "output_tokens")


@dataclass(frozen=True)
class Token_Usage:
    """Tokens consumed by one provider request."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def _first_int(source: Any, names: tuple[str, ...]) -> int | None:
    """The first of ``names`` present on ``source`` as a non-negative int, else ``None``.

    ``None`` is distinct from ``0``: absent means the provider did not tell us, whereas zero
    would be a claim that the request consumed nothing.
    """
    for name in names:
        raw = getattr(source, name, None)
        if raw is None and isinstance(source, dict):
            raw = source.get(name)
        if raw is None:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return value
    return None


def extract_usage(response: Any) -> Token_Usage | None:
    """Token usage from a provider response, or ``None`` if it does not carry any.

    Total, by design: this runs on the success path of every LLM call, and a change in an SDK's
    response shape must cost an accounting row rather than the render. Anything unexpected
    yields ``None``, which the caller records as an *unmetered* call - so the call is still
    counted and the shortfall is visible rather than absorbed into a plausible total.
    """
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return None
    prompt = _first_int(usage, _PROMPT_FIELDS)
    completion = _first_int(usage, _COMPLETION_FIELDS)
    if prompt is None and completion is None:
        return None
    return Token_Usage(prompt_tokens=prompt or 0, completion_tokens=completion or 0)


def _rates() -> tuple[float, float] | None:
    """Configured (input, output) price per million tokens, or ``None`` if unpriced.

    Either rate being set counts as priced - a model that charges for input and not output is
    unusual but expressible, and requiring both would refuse to cost it at all.
    """
    input_rate = max(0.0, float(settings.llm_price_input_per_mtok))
    output_rate = max(0.0, float(settings.llm_price_output_per_mtok))
    if input_rate <= 0.0 and output_rate <= 0.0:
        return None
    return input_rate, output_rate


@dataclass
class Model_Usage:
    """Accumulated usage for one model within one job."""

    model: str
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: Calls whose response carried no readable usage. Counted separately so ``calls`` stays
    #: truthful while making clear that the token totals - and therefore any cost - understate
    #: reality. Folding these in as zero-token calls would hide the gap inside a number that
    #: still looked like a complete answer.
    unmetered_calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def cost_usd(self) -> float | None:
        """Spend for this model, or ``None`` when no rate is configured."""
        rates = _rates()
        if rates is None:
            return None
        input_rate, output_rate = rates
        cost = (self.prompt_tokens * input_rate + self.completion_tokens * output_rate) / 1_000_000
        # Six places: a single cheap call can genuinely cost a fraction of a cent, and rounding
        # to two would report most individual jobs as costing nothing - which is the same
        # false-zero this module exists to avoid.
        return round(cost, 6)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "unmetered_calls": self.unmetered_calls,
            "cost_usd": self.cost_usd(),
        }


@dataclass
class Job_LLM_Usage:
    """Every model's usage within one job.

    Locked for the reason ``Job_Metrics`` is: calls are recorded from the worker thread while
    the API may serialise this for a progress response at any moment, and a read during a write
    would not raise - it would report a wrong number, which in a cost report is worse than no
    number because it gets believed.
    """

    models: dict[str, Model_Usage] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, model: str, usage: Token_Usage | None) -> None:
        name = str(model or "unknown")
        with self._lock:
            entry = self.models.get(name)
            if entry is None:
                entry = Model_Usage(name)
                self.models[name] = entry
            entry.calls += 1
            if usage is None:
                entry.unmetered_calls += 1
            else:
                entry.prompt_tokens += max(0, usage.prompt_tokens)
                entry.completion_tokens += max(0, usage.completion_tokens)

    def to_dict(self) -> dict[str, Any]:
        """Totals plus a per-model breakdown, cheapest-first ordering avoided in favour of cost.

        Sorted by total tokens descending, so the model doing the work is the first row.
        """
        with self._lock:
            entries = sorted(self.models.values(), key=lambda m: -m.total_tokens)
            rows = [m.to_dict() for m in entries]
        costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
        return {
            "calls": sum(r["calls"] for r in rows),
            "prompt_tokens": sum(r["prompt_tokens"] for r in rows),
            "completion_tokens": sum(r["completion_tokens"] for r in rows),
            "total_tokens": sum(r["total_tokens"] for r in rows),
            "unmetered_calls": sum(r["unmetered_calls"] for r in rows),
            # None when nothing is priced, so a consumer can tell "no rate configured" from
            # "no spend". Present but understated when unmetered_calls > 0, which is why that
            # count travels alongside rather than being reported only in the log.
            "cost_usd": round(sum(costs), 6) if costs else None,
            "priced": _rates() is not None,
            "models": rows,
        }


#: Usage per job id. Bounded for the same reason ``observability._metrics`` is - process-global
#: state that grows once per job is a leak that looks like nothing - and bounded by *that*
#: module's constant rather than a second one, because two limits on the same lifetime would
#: drift and the smaller would silently win.
_usage: dict[str, Job_LLM_Usage] = {}
_usage_lock = threading.Lock()


def usage_for(job_id: str) -> Job_LLM_Usage:
    """The usage record for ``job_id``, creating it if needed."""
    with _usage_lock:
        found = _usage.get(job_id)
        if found is None:
            found = Job_LLM_Usage()
            _usage[job_id] = found
            if len(_usage) > observability.MAX_TRACKED_JOBS:
                oldest = next(iter(_usage))
                if oldest != job_id:
                    _usage.pop(oldest, None)
        return found


def clear_usage(job_id: str | None = None) -> None:
    """Forget one job's usage, or all of it (used by tests)."""
    with _usage_lock:
        if job_id is None:
            _usage.clear()
        else:
            _usage.pop(job_id, None)


def snapshot() -> dict[str, dict[str, Any]]:
    """Every tracked job's usage, for cross-job aggregation (``/metrics``).

    A copy of the keys is taken under the lock and each record serialised outside it, so a long
    aggregation cannot block a render recording a call.
    """
    with _usage_lock:
        tracked = list(_usage.items())
    return {job_id: record.to_dict() for job_id, record in tracked}


def record_response(model: str, response: Any, job_id: str | None = None) -> None:
    """Record one provider call against the current job.

    Called from the provider clients where the response object is still in scope. Silent and
    total: accounting is telemetry, and a failure to count tokens must never turn a successful
    completion into a failed one. The job is resolved from the observability context, which the
    worker thread has already entered, so no call site has to pass an id.
    """
    try:
        job = job_id if job_id is not None else observability.current_job_id()
        if not job:
            # An LLM call outside a job - a capability probe, or a test. Nothing to attribute
            # it to, and inventing a bucket would put real spend under a fake job id.
            return
        usage_for(job).record(model, extract_usage(response))
    except Exception:  # pragma: no cover - defensive; accounting must not break a render
        logger.debug("could not record LLM usage", exc_info=True)
