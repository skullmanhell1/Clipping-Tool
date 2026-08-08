"""Best-time-to-post suggestions and scheduling helpers (PB7).

**Read this before trusting the numbers.** These are *published third-party heuristics*, not
measurements of your audience. Nothing in this repo yet collects post-publish engagement - that is
`PB8`, and it is the only thing that could turn a suggestion into a fact. Until it exists, the
honest description of this module is "a sensible default starting point, drawn from the windows
social-media research consistently reports", and the API labels it that way so the UI cannot
present a guess as an analysis.

Reported windows for short-form video cluster around late morning and early evening on weekdays,
with platform-specific differences - TikTok skewing later, LinkedIn-style professional feeds
skewing to the working day. The tables below encode that. They are deliberately coarse (whole and
half hours, a handful per platform) because pretending to 15-minute precision from a blog-post
consensus would be false precision.

Two design notes:

* times are held as **local** hours and converted using the machine's timezone, because "post at
  7pm" means 7pm where the audience is, and a UTC table would silently be wrong for every operator
  outside UTC;
* a suggestion is never in the past. A calendar that offers this morning's slot at 4pm is
  offering nothing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

#: Suggested local posting times per platform, as ``(hour, minute)`` pairs.
#:
#: Ordered best-first within a day, so a caller taking the first entry gets the strongest slot.
PLATFORM_WINDOWS: dict[str, tuple[tuple[int, int], ...]] = {
    # Short-form vertical feeds: evening-heavy, with a lunchtime secondary peak.
    "tiktok": ((19, 0), (12, 0), (21, 0), (9, 0)),
    "instagram": ((18, 30), (11, 0), (20, 0), (13, 0)),
    "youtube_shorts": ((17, 0), (12, 0), (20, 0), (10, 0)),
    # YouTube proper is watched later and longer; the afternoon upload catches the evening session.
    "youtube": ((15, 0), (17, 0), (12, 0), (20, 0)),
    # X is a working-hours conversation more than an evening one.
    "x": ((9, 0), (12, 0), (17, 0), (15, 0)),
    # Whop communities are commercial: weekday business hours.
    "whop": ((10, 0), (14, 0), (16, 0), (12, 0)),
}

#: Used for any platform without an entry - the broad consensus window.
DEFAULT_WINDOWS: tuple[tuple[int, int], ...] = ((18, 0), (12, 0), (9, 0), (20, 0))

#: Weekdays (``Monday=0``) that are generally weaker for reach, used only to rank equal slots.
#:
#: Saturday and Sunday are consistently reported as lower-reach for the professional and
#: commercial feeds and roughly neutral for the entertainment ones. Rather than encode that per
#: platform from thin evidence, weekends are simply ranked below weekdays and never excluded.
WEAKER_DAYS: frozenset[int] = frozenset({5, 6})

#: What these suggestions are based on, returned with them so a UI cannot imply otherwise.
BASIS = (
    "Published third-party posting-time heuristics, not measured from your audience. "
    "Per-account measurement needs post-publish engagement data (PB8), which this "
    "installation does not collect yet."
)


def windows_for(platform: str) -> tuple[tuple[int, int], ...]:
    """The suggested local time-of-day windows for ``platform``, best first."""
    return PLATFORM_WINDOWS.get((platform or "").strip().lower(), DEFAULT_WINDOWS)


@dataclass(frozen=True)
class Suggestion:
    """One suggested posting slot."""

    #: Unix timestamp of the slot.
    at: float
    #: Local ISO-8601 rendering, so a client does not have to re-derive the timezone.
    local: str
    platform: str
    #: 0..1, relative rank among the returned suggestions. Not a predicted engagement rate.
    rank: float
    weekday: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "local": self.local,
            "platform": self.platform,
            "rank": self.rank,
            "weekday": self.weekday,
        }


def suggest(
    platform: str,
    *,
    days: int = 7,
    per_day: int = 2,
    now: float | None = None,
    taken: list[float] | None = None,
    spacing_seconds: float = 3600.0,
) -> list[Suggestion]:
    """Suggested posting slots for the next ``days`` days (PB7).

    ``taken`` lists timestamps already scheduled; a slot within ``spacing_seconds`` of one is
    skipped. Without that, the calendar keeps recommending the single best hour that is already
    full, and following its advice stacks four posts on one platform at 7pm - which is worse for
    reach than any of the times it rejected.

    Slots in the past are never returned.
    """
    reference = time.time() if now is None else float(now)
    horizon = max(1, int(days))
    wanted_per_day = max(1, int(per_day))
    busy = sorted(float(t) for t in (taken or []))

    windows = windows_for(platform)
    start = datetime.fromtimestamp(reference)
    found: list[Suggestion] = []

    for offset in range(horizon):
        day = (start + timedelta(days=offset)).date()
        chosen_today = 0
        for index, (hour, minute) in enumerate(windows):
            if chosen_today >= wanted_per_day:
                break
            slot = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)
            at = slot.timestamp()
            if at <= reference:
                continue
            if any(abs(at - t) < spacing_seconds for t in busy):
                continue
            # Rank: earlier windows score higher, later days decay, weekends take a small penalty.
            rank = 1.0 - (index * 0.1) - (offset * 0.05)
            if slot.weekday() in WEAKER_DAYS:
                rank -= 0.1
            found.append(
                Suggestion(
                    at=at,
                    local=slot.isoformat(timespec="minutes"),
                    platform=(platform or "").strip().lower(),
                    rank=round(max(0.0, min(1.0, rank)), 3),
                    weekday=slot.weekday(),
                )
            )
            chosen_today += 1
            busy.append(at)

    found.sort(key=lambda s: (-s.rank, s.at))
    return found
