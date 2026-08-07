"""Saved settings profiles, and the read-only built-in profile bundles."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from api.routers._models import ProfileModel
from profiles import get_profile_store
from worker.models import BUILTIN_PROFILES

router = APIRouter()


# ---------------------------------------------------------------------------
# Saved settings profiles
# ---------------------------------------------------------------------------
@router.get("/api/profiles/builtin", tags=["profiles"])
def list_builtin_profiles() -> dict:
    """The shipped opinionated profiles (U2).

    Distinct from ``GET /api/profiles``, which lists profiles a *user* saved. These are
    read-only bundles: pass ``profile: "<name>"`` alongside a process request and the bundle
    is expanded into the individual options, with anything else in the request overriding it.

    ``settings`` is returned in full rather than summarised so a client can show what
    picking a profile will actually change, and ``rationale`` is included because a profile
    is a set of judgement calls a user is entitled to disagree with.
    """
    return {
        "profiles": [
            {
                "name": profile.name,
                "label": profile.label,
                "description": profile.description,
                "rationale": profile.rationale,
                "settings": dict(profile.settings),
            }
            for profile in BUILTIN_PROFILES.values()
        ]
    }


@router.get("/api/profiles", tags=["profiles"])
def list_profiles() -> dict:
    store = get_profile_store()
    default = store.get_default()
    return {
        "profiles": [p.to_dict() for p in store.list()],
        "default_id": default.id if default else None,
    }


@router.post("/api/profiles", tags=["profiles"])
def save_profile(req: ProfileModel) -> dict:
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Profile name is required")
    prof = get_profile_store().save(
        req.name, req.settings, req.publishing,
        profile_id=req.id, make_default=req.make_default,
    )
    return prof.to_dict()


@router.post("/api/profiles/{profile_id}/default", tags=["profiles"])
def set_default_profile(profile_id: str) -> dict:
    prof = get_profile_store().set_default(profile_id)
    if prof is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return prof.to_dict()


@router.delete("/api/profiles/{profile_id}", tags=["profiles"])
def delete_profile(profile_id: str) -> dict:
    if not get_profile_store().delete(profile_id):
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"deleted": True, "id": profile_id}
