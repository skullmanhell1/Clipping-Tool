"""Read-only delivery readiness for one clip.

The publish worker chooses a file at submission time. This endpoint exposes the same decision
before a button is pressed, including facts a browser cannot know: whether the variant file is
still on disk, which account the campaign route resolves to, and whether that publisher can
actually auto-publish.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from config import settings
from publishers import resolve_platform
from publishers.delivery import select_delivery
from publishers.history import get_history
from publishers.manager import get_publish_manager
from worker import output_profiles
from worker.jobs import get_manager

router = APIRouter()


def _requested_platforms(raw: str) -> list[str]:
    requested = [
        item.strip() for item in (raw or "").replace("\n", ",").split(",") if item.strip()
    ]
    if len(requested) > 12:
        raise HTTPException(status_code=400, detail="Ask for at most 12 destinations at once")
    if requested:
        return list(dict.fromkeys(requested))
    return [
        str(item.get("name"))
        for item in output_profiles.available_profiles()
        if item.get("name")
    ]


def _publisher_readiness(
    publisher_name: str, label: str, account_id: str
) -> tuple[dict[str, Any], list[str]]:
    """Return the publisher status and every reason it cannot auto-publish."""
    publisher = get_publish_manager().publishers.get(publisher_name)
    if publisher is None:
        return {}, [f"No publisher is registered for {publisher_name}."]
    try:
        status = publisher.status(account_id)
    except Exception as exc:  # pragma: no cover - a broken publisher must not break preview
        return {"configured": False, "direct_publish": False}, [
            f"Could not read the {label} publisher status: {exc}"
        ]
    blockers = []
    if not status.configured:
        blockers.append(
            f"{label} is not configured: {status.message or 'credentials are missing'}"
        )
    elif not status.direct_publish:
        blockers.append(
            f"{label} only supports review or limited publishing: "
            f"{status.message or 'direct auto publishing is unavailable'}"
        )
    return status.to_dict(), blockers


@router.get("/api/jobs/{job_id}/clips/{clip_id}/delivery-readiness", tags=["publishing"])
def delivery_readiness(
    job_id: str,
    clip_id: str,
    platforms: str = "",
    campaign_id: str = "",
) -> dict:
    """Describe what each destination will receive, before anything is queued.

    The result is advisory except for honest blockers: duration, missing files, and an
    unknown or unconfigured publisher. A canonical fallback is not a blocker because
    publishing it is valid, but it is called out with the action that would remove the
    compromise.
    """
    manager = get_manager()
    job = manager.store.get(job_id)
    clip = manager.store.get_clip(job_id, clip_id)
    if job is None or clip is None:
        raise HTTPException(status_code=404, detail="Job or clip not found")

    campaign = get_history().campaign(campaign_id) if campaign_id else None
    routes = dict(getattr(campaign, "routes", {}) or {})
    canonical_path = Path(settings.clips_dir) / job_id / clip.filename
    destinations = []

    for requested_platform in _requested_platforms(platforms):
        requested_platform = requested_platform.casefold()
        publisher_name = resolve_platform(requested_platform)
        profile_platform = (
            "instagram" if requested_platform in {"instagram_reels", "reels"} else requested_platform
        )
        profile = output_profiles.profile_for(profile_platform)
        label = output_profiles.label_for(profile_platform)
        route = routes.get(requested_platform) or routes.get(publisher_name) or {}
        account_id = str(route.get("account_id") or "")
        publisher_status, blockers = _publisher_readiness(
            publisher_name, label, account_id
        )
        warnings: list[str] = []

        delivery_path, delivery = select_delivery(clip, requested_platform, canonical_path)
        file_exists = delivery_path.is_file()
        if not file_exists:
            blockers.append(f"The delivery file {delivery.get('filename', '')} is missing.")

        max_duration_s = profile.max_duration_s if profile is not None else None
        clip_duration_s = float(getattr(clip, "duration", 0.0) or 0.0)
        if max_duration_s and clip_duration_s > float(max_duration_s):
            blockers.append(
                f"The clip is {clip_duration_s:.0f}s, longer than {label}'s "
                f"{float(max_duration_s):.0f}s limit."
            )

        if delivery.get("kind") == "variant":
            width = delivery.get("width")
            height = delivery.get("height")
            if profile is not None and width and height:
                expected = profile.size
                if (int(width), int(height)) != expected:
                    warnings.append(
                        f"The {label} variant is {width}×{height}, but this destination expects "
                        f"{expected[0]}×{expected[1]}."
                    )
        else:
            if delivery.get("fallback") == "variant_file_missing":
                warnings.append(
                    f"The {label} variant file {delivery.get('variant_filename', '')} is missing; "
                    "the canonical clip will be sent."
                )
            else:
                warnings.append(
                    f"No {label} variant is rendered; the canonical clip will be sent with "
                    "its original geometry."
                )

        action = "none"
        if not file_exists:
            action = "restore_file"
        elif blockers:
            action = (
                "shorten_clip"
                if max_duration_s and clip_duration_s > float(max_duration_s)
                else "configure_publisher"
            )
        elif delivery.get("kind") == "canonical":
            action = "render_variant"

        destinations.append(
            {
                "requested_platform": requested_platform,
                "publisher": publisher_name,
                "label": label,
                "account_id": account_id,
                "publisher_status": publisher_status,
                "status": (
                    "blocked"
                    if blockers
                    else ("fallback" if delivery.get("kind") == "canonical" else "ready")
                ),
                "publishable": not blockers and file_exists,
                "delivery": delivery,
                "filename": delivery.get("filename", ""),
                "file_exists": file_exists,
                "clip_duration_s": clip_duration_s,
                "max_duration_s": max_duration_s,
                "width": delivery.get("width") if delivery.get("kind") == "variant" else None,
                "height": delivery.get("height") if delivery.get("kind") == "variant" else None,
                "aspect": delivery.get("aspect") if delivery.get("kind") == "variant" else None,
                "blockers": blockers,
                "warnings": warnings,
                "action": action,
            }
        )

    return {
        "job_id": job_id,
        "clip_id": clip_id,
        "clip_filename": clip.filename,
        "campaign_id": campaign_id,
        "destinations": destinations,
        "summary": {
            "ready": sum(1 for item in destinations if item["status"] == "ready"),
            "fallback": sum(1 for item in destinations if item["status"] == "fallback"),
            "blocked": sum(1 for item in destinations if item["status"] == "blocked"),
        },
    }
