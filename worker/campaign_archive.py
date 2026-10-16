"""Idempotent campaign expiry and local/cloud archival helpers."""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any


def is_expired(promotional_end: str, *, today: date | None = None) -> bool:
    if not promotional_end:
        return False
    try:
        end = date.fromisoformat(str(promotional_end)[:10])
    except ValueError:
        return False
    return (today or date.today()) > end


def archive_campaign(
    source_dir: str | Path,
    archive_dir: str | Path,
    campaign_id: str,
    *,
    promotional_end: str = "",
    today: date | None = None,
    cloud_archive: Callable[[Path, str], Any] | None = None,
) -> dict[str, Any]:
    """Archive a campaign only after expiry; repeated calls return the same marker."""
    source = Path(source_dir)
    destination = Path(archive_dir) / str(campaign_id)
    marker = destination / ".archive.json"
    if marker.is_file():
        try:
            return json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    if not is_expired(promotional_end, today=today):
        return {"campaign_id": campaign_id, "archived": False, "reason": "not_expired"}
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.exists():
        if destination.exists():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            shutil.move(str(source), str(destination))
    if cloud_archive is not None and destination.exists():
        cloud_archive(destination, campaign_id)
    result = {
        "campaign_id": campaign_id,
        "archived": True,
        "archived_at": time.time(),
        "path": str(destination),
    }
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def archive_campaign_from_brief(
    source_dir: str | Path,
    archive_dir: str | Path,
    campaign_id: str,
    brief: Any,
    *,
    today: date | None = None,
    cloud_archive: Callable[[Path, str], Any] | None = None,
) -> dict[str, Any]:
    """Archive using the latest end date in an existing ``CampaignBrief``."""
    dates = list(getattr(brief, "promotional_dates", []) or [])
    ends = [str(getattr(item, "end_date", "") or getattr(item, "start_date", "")) for item in dates]
    ends = [value for value in ends if value]
    return archive_campaign(
        source_dir,
        archive_dir,
        campaign_id,
        promotional_end=max(ends) if ends else "",
        today=today,
        cloud_archive=cloud_archive,
    )


def archive_expired_history_campaigns(
    history: Any,
    archive_root: str | Path,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Copy expired campaign clips from publish history into an idempotent archive tree."""
    archived: list[str] = []
    for campaign in history.campaigns():
        if not campaign.brief or not is_expired(
            max(
                [
                    str(getattr(item, "end_date", "") or getattr(item, "start_date", ""))
                    for item in campaign.brief.promotional_dates
                    if getattr(item, "end_date", "") or getattr(item, "start_date", "")
                ],
                default="",
            ),
            today=today,
        ):
            continue
        destination = Path(archive_root) / campaign.id
        marker = destination / ".archive.json"
        if marker.exists():
            archived.append(campaign.id)
            continue
        rows = [
            row
            for row in history.history(limit=100000).get("clips", [])
            if row.get("campaign_id") == campaign.id
        ]
        destination.mkdir(parents=True, exist_ok=True)
        copied = 0
        for row in rows:
            source = Path(str(row.get("path") or ""))
            if source.is_file():
                shutil.copy2(source, destination / source.name)
                copied += 1
        marker.write_text(
            json.dumps(
                {
                    "campaign_id": campaign.id,
                    "archived": True,
                    "copied": copied,
                    "archived_at": time.time(),
                }
            ),
            encoding="utf-8",
        )
        archived.append(campaign.id)
    return {"archived_campaigns": archived, "count": len(archived)}
