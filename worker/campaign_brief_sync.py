"""Polling/webhook-ready synchronisation for campaign briefs.

The service is intentionally transport-neutral.  A poller can call :meth:`sync`; a Drive push
handler can pass the same file id/revision and optional document text to
:meth:`sync_from_webhook`.  The service compares both Drive revision metadata and a SHA-256 content
hash, persists the parsed brief in the existing campaign history database, and marks existing clips
for re-render and human review.  It never auto-publishes or changes a job with no configured brief.

All network and job-store collaborators are injectable, which keeps the tests offline and avoids a
new dependency or secret requirement.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from worker.campaign_brief import CampaignBrief, parse_campaign_brief

logger = logging.getLogger(__name__)


class BriefSyncError(RuntimeError):
    """Raised for an invalid sync request, not for a transient Drive failure."""


@dataclass(frozen=True)
class DriveBriefSnapshot:
    """A text snapshot returned by a Drive reader or a webhook payload."""

    file_id: str
    revision_id: str = ""
    content: str = ""
    content_hash: str = ""
    modified_time: str = ""
    mime_type: str = ""

    def __post_init__(self) -> None:
        if not self.content_hash:
            object.__setattr__(
                self,
                "content_hash",
                hashlib.sha256(self.content.encode("utf-8")).hexdigest(),
            )

    @classmethod
    def from_content(
        cls,
        file_id: str,
        content: str,
        *,
        revision_id: str = "",
        modified_time: str = "",
        mime_type: str = "",
    ) -> DriveBriefSnapshot:
        return cls(
            file_id=str(file_id or "").strip(),
            revision_id=str(revision_id or "").strip(),
            content=str(content or ""),
            modified_time=str(modified_time or "").strip(),
            mime_type=str(mime_type or "").strip(),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "file_id": self.file_id,
            "revision_id": self.revision_id,
            "content_hash": self.content_hash,
            "modified_time": self.modified_time,
            "mime_type": self.mime_type,
        }


class BriefDriveReader(Protocol):
    """Minimal collaborator needed by polling; suitable for a Drive API or webhook adapter."""

    def fetch(
        self,
        file_id: str,
        *,
        known_revision: str = "",
        known_content_hash: str = "",
    ) -> DriveBriefSnapshot: ...


class GoogleDriveBriefReader:
    """Authenticated reader backed by the existing Google Drive helper."""

    def fetch(
        self,
        file_id: str,
        *,
        known_revision: str = "",
        known_content_hash: str = "",
    ) -> DriveBriefSnapshot:
        # Metadata is read first so invalid ids and non-document files fail before any export.
        from worker.google_drive import download_text_file, get_file_metadata

        metadata = get_file_metadata(file_id, require_video=False)
        revision = str(
            metadata.get("headRevisionId")
            or metadata.get("version")
            or metadata.get("modifiedTime")
            or ""
        )
        content, metadata = download_text_file(
            file_id, mime_type=str(metadata.get("mimeType") or "")
        )
        snapshot = DriveBriefSnapshot.from_content(
            file_id,
            content,
            revision_id=revision,
            modified_time=str(metadata.get("modifiedTime") or ""),
            mime_type=str(metadata.get("mimeType") or ""),
        )
        # The known values are accepted as part of the seam for polling adapters.  The content is
        # still fetched and hashed: Google Docs revisions are not a substitute for content identity.
        return snapshot


DriveBriefSource = BriefDriveReader


@dataclass
class BriefSyncResult:
    """A durable, inspectable outcome of one sync attempt."""

    campaign_id: str
    status: str
    changed: bool = False
    source_file_id: str = ""
    revision_id: str = ""
    content_hash: str = ""
    parser: str = ""
    affected_clip_ids: list[dict[str, str]] = field(default_factory=list)
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def affected_clips(self) -> list[dict[str, str]]:
        return self.affected_clip_ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "status": self.status,
            "changed": self.changed,
            "source_file_id": self.source_file_id,
            "revision_id": self.revision_id,
            "content_hash": self.content_hash,
            "parser": self.parser,
            "affected_clip_ids": list(self.affected_clip_ids),
            "error": self.error,
            "warnings": list(self.warnings),
        }


class CampaignBriefSyncService:
    """Synchronise one campaign brief with durable campaign and clip state."""

    def __init__(
        self,
        campaign_store: Any,
        *,
        job_store: Any = None,
        drive_reader: BriefDriveReader | None = None,
        parser: Any = parse_campaign_brief,
        clock: Any = time.time,
    ) -> None:
        self.campaign_store = campaign_store
        self.job_store = job_store
        self.drive_reader = drive_reader or GoogleDriveBriefReader()
        self.parser = parser
        self.clock = clock

    def sync(
        self,
        campaign_id: str,
        *,
        source_file_id: str | None = None,
        snapshot: DriveBriefSnapshot | None = None,
        raw_text: str | None = None,
        client: Any = None,
        llm_client: Any = None,
        force: bool = False,
    ) -> BriefSyncResult:
        """Poll or apply one brief snapshot.

        ``snapshot``/``raw_text`` are the webhook seam; when neither is supplied the injected Drive
        reader is polled.  Unchanged revision/content returns without reparsing or flagging clips.
        A source/read failure is recorded as an error while retaining the last good brief.
        """
        campaign = self._campaign(campaign_id)
        if campaign is None:
            raise BriefSyncError(f"Campaign not found: {campaign_id}")
        file_id = str(source_file_id or getattr(campaign, "brief_source_file_id", "") or "").strip()
        if snapshot is None and raw_text is None and not file_id:
            return BriefSyncResult(campaign_id=campaign_id, status="unconfigured")
        if snapshot is None and raw_text is not None:
            snapshot = DriveBriefSnapshot.from_content(
                file_id or str(getattr(campaign, "brief_source_file_id", "") or ""),
                raw_text,
            )
        if snapshot is None:
            try:
                snapshot = self.drive_reader.fetch(
                    file_id,
                    known_revision=str(getattr(campaign, "brief_revision", "") or ""),
                    known_content_hash=str(getattr(campaign, "brief_content_hash", "") or ""),
                )
            except Exception as exc:
                message = str(exc) or type(exc).__name__
                self._record_error(campaign, file_id, message)
                return BriefSyncResult(
                    campaign_id=campaign_id,
                    status="error",
                    source_file_id=file_id,
                    error=message,
                )

        if not snapshot.file_id:
            snapshot = DriveBriefSnapshot.from_content(
                file_id, snapshot.content, revision_id=snapshot.revision_id
            )
        same_revision = bool(
            snapshot.revision_id
            and snapshot.revision_id == str(getattr(campaign, "brief_revision", "") or "")
        )
        same_content = bool(
            snapshot.content_hash
            and snapshot.content_hash == str(getattr(campaign, "brief_content_hash", "") or "")
        )
        same_source = snapshot.file_id == file_id or not file_id
        if not force and same_source and same_revision and same_content:
            return BriefSyncResult(
                campaign_id=campaign_id,
                status="unchanged",
                source_file_id=snapshot.file_id,
                revision_id=snapshot.revision_id,
                content_hash=snapshot.content_hash,
            )

        try:
            try:
                brief = self.parser(snapshot.content, client=client, llm_client=llm_client)
            except TypeError as exc:
                # A tiny injected parser is useful in a webhook adapter/test and may only accept
                # the document text.  Do not force that collaborator to mirror the LLM kwargs.
                if "unexpected keyword" not in str(exc):
                    raise
                brief = self.parser(snapshot.content)
            if not isinstance(brief, CampaignBrief):
                brief = CampaignBrief.from_dict(brief if isinstance(brief, dict) else {})
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            self._record_error(campaign, snapshot.file_id or file_id, message)
            return BriefSyncResult(
                campaign_id=campaign_id,
                status="error",
                source_file_id=snapshot.file_id or file_id,
                revision_id=snapshot.revision_id,
                content_hash=snapshot.content_hash,
                error=message,
            )

        revision = snapshot.revision_id or snapshot.content_hash
        content_hash = snapshot.content_hash
        updated = self.campaign_store.update_campaign_brief(
            campaign_id,
            brief,
            source_file_id=snapshot.file_id or file_id,
            revision=revision,
            content_hash=content_hash,
            synced_at=self.clock(),
            status="synced",
            error="",
        )
        if updated is None:
            raise BriefSyncError(f"Campaign disappeared during sync: {campaign_id}")

        note = (
            f"Campaign brief updated ({revision or content_hash[:12]}). "
            "Re-render and review this clip before publishing."
        )
        affected = self._flag_clips(
            campaign_id,
            brief_revision=revision,
            brief_content_hash=content_hash,
            review_note=note,
        )
        return BriefSyncResult(
            campaign_id=campaign_id,
            status="synced",
            changed=True,
            source_file_id=snapshot.file_id or file_id,
            revision_id=revision,
            content_hash=content_hash,
            parser=brief.parser,
            affected_clip_ids=affected,
            warnings=list(brief.warnings),
        )

    def poll(self, campaign_id: str, **kwargs: Any) -> BriefSyncResult:
        """Polling-friendly alias for :meth:`sync`."""
        return self.sync(campaign_id, **kwargs)

    def sync_from_webhook(
        self,
        campaign_id: str,
        payload: dict[str, Any],
        *,
        client: Any = None,
        llm_client: Any = None,
    ) -> BriefSyncResult:
        """Apply a Drive push payload without trusting it as the document contents.

        Push payloads may include text for low-latency handling, but callers can omit it and the
        service will poll the Drive reader.  Revision ids are metadata only; content is always
        hashed before it can replace the durable brief.
        """
        payload = payload or {}
        file_id = str(
            payload.get("file_id") or payload.get("drive_file_id") or payload.get("id") or ""
        ).strip()
        raw_text = payload.get("text", payload.get("content"))
        if raw_text is not None:
            snapshot = DriveBriefSnapshot.from_content(
                file_id,
                str(raw_text),
                revision_id=str(
                    payload.get("revision_id")
                    or payload.get("revision")
                    or payload.get("version")
                    or ""
                ),
                modified_time=str(
                    payload.get("modified_time") or payload.get("modifiedTime") or ""
                ),
                mime_type=str(payload.get("mime_type") or payload.get("mimeType") or ""),
            )
            return self.sync(
                campaign_id,
                source_file_id=file_id or None,
                snapshot=snapshot,
                client=client,
                llm_client=llm_client,
            )
        return self.sync(
            campaign_id,
            source_file_id=file_id or None,
            client=client,
            llm_client=llm_client,
        )

    def _campaign(self, campaign_id: str) -> Any:
        getter = getattr(self.campaign_store, "campaign", None) or getattr(
            self.campaign_store, "get_campaign", None
        )
        if getter is None:
            raise BriefSyncError("campaign_store must provide campaign() or get_campaign()")
        return getter(campaign_id)

    def _record_error(self, campaign: Any, source_file_id: str, message: str) -> None:
        try:
            self.campaign_store.update_campaign_brief(
                campaign.id,
                getattr(campaign, "brief", None),
                source_file_id=source_file_id,
                revision=str(getattr(campaign, "brief_revision", "") or ""),
                content_hash=str(getattr(campaign, "brief_content_hash", "") or ""),
                synced_at=getattr(campaign, "brief_synced_at", None),
                status="error",
                error=message,
            )
        except Exception:
            logger.exception("could not persist campaign brief sync error for %s", campaign.id)

    def _flag_clips(
        self,
        campaign_id: str,
        *,
        brief_revision: str,
        brief_content_hash: str,
        review_note: str,
    ) -> list[dict[str, str]]:
        affected: list[dict[str, str]] = []
        try:
            affected.extend(
                self.campaign_store.flag_campaign_clips(
                    campaign_id,
                    brief_revision=brief_revision,
                    brief_content_hash=brief_content_hash,
                    review_note=review_note,
                )
            )
        except Exception:
            logger.exception("could not flag history clips for campaign %s", campaign_id)

        job_store = self.job_store
        if job_store is None:
            # Lazy default keeps importing this module side-effect free.  A webhook/poller that is
            # actually used in the app can discover the live JobStore without making it mandatory
            # for offline parsing/tests.
            try:
                from worker.jobs import get_manager

                job_store = get_manager().store
            except Exception:
                job_store = None
        if job_store is None:
            return _dedupe_clip_ids(affected)

        try:
            jobs = job_store.all()
        except Exception:
            logger.exception("could not enumerate jobs for campaign %s", campaign_id)
            return _dedupe_clip_ids(affected)
        for job in jobs:
            options = getattr(job, "options", None)
            campaign_ids = {
                str(getattr(options, "campaign_id", "") or ""),
                str(getattr(options, "brief_id", "") or ""),
            }
            if campaign_id not in campaign_ids:
                continue
            for clip in list(getattr(job, "clips", []) or []):
                updated = job_store.update_clip(
                    job.id,
                    clip.id,
                    {
                        "needs_rerender": True,
                        "brief_revision": brief_revision,
                        "brief_content_hash": brief_content_hash,
                        "review_state": "pending",
                        "review_note": review_note,
                    },
                )
                if updated is not None:
                    affected.append({"job_id": str(job.id), "clip_id": str(clip.id)})
                    try:
                        self.campaign_store.sync_clip(job.id, updated)
                    except Exception:
                        logger.exception(
                            "could not mirror brief flag for clip %s/%s", job.id, clip.id
                        )
        return _dedupe_clip_ids(affected)


def _dedupe_clip_ids(values: list[dict[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in values:
        key = (str(item.get("job_id", "")), str(item.get("clip_id", "")))
        if key not in seen:
            seen.add(key)
            result.append({"job_id": key[0], "clip_id": key[1]})
    return result


CampaignBriefSync = CampaignBriefSyncService


def sync_campaign_brief(
    service: CampaignBriefSyncService, campaign_id: str, **kwargs: Any
) -> BriefSyncResult:
    """Functional adapter for a scheduler that owns a service instance."""
    return service.sync(campaign_id, **kwargs)
