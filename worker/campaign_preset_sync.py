"""Review-only Google Drive campaign preset import.

The service accepts a user-provided Google Docs/Drive URL, extracts only a validated file ID,
reads the document through the existing authenticated Drive reader, and parses the text into the
existing :class:`CampaignPreset` model. It deliberately has no campaign-store dependency: importing
or applying a preset never creates or saves a publish campaign.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from worker.campaign_brief_sync import DriveBriefSnapshot, GoogleDriveBriefReader
from worker.campaign_preset import CampaignPreset, parse_campaign_preset
from worker.google_drive import (
    GoogleDriveError,
    configured,
    download_public_text_file,
    extract_file_id,
)

logger = logging.getLogger(__name__)


class CampaignPresetImportError(RuntimeError):
    """A safe, user-actionable failure while importing a Drive campaign preset."""

    def __init__(self, message: str, *, code: str, http_status: int) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True)
class CampaignPresetImportResult:
    """A reviewable preset plus non-secret source and synchronisation provenance."""

    preset: CampaignPreset
    source_url: str
    source_file_id: str
    source_type: str
    sync_status: str
    revision_id: str = ""
    content_hash: str = ""
    modified_time: str = ""
    mime_type: str = ""
    parser: str = "deterministic"
    synced_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        provenance = {
            "source_type": self.source_type,
            "source_url": self.source_url,
            "file_id": self.source_file_id,
            "revision_id": self.revision_id,
            "content_hash": self.content_hash,
            "modified_time": self.modified_time,
            "mime_type": self.mime_type,
        }
        sync = {
            "status": self.sync_status,
            "parser": self.parser,
            "synced_at": self.synced_at,
            "revision_id": self.revision_id,
            "content_hash": self.content_hash,
        }
        return {
            "preset": self.preset.to_dict(),
            "requires_review": True,
            "provenance": provenance,
            "source": provenance,
            "sync": sync,
            # These flat aliases make the response easy for existing clients to inspect while the
            # nested blocks keep the source and status meanings explicit for new clients.
            "source_file_id": self.source_file_id,
            "source_url": self.source_url,
            "status": self.sync_status,
            "sync_status": self.sync_status,
            "changed": True,
            "revision_id": self.revision_id,
            "content_hash": self.content_hash,
            "parser": self.parser,
            "synced_at": self.synced_at,
        }


class CampaignPresetDriveService:
    """Fetch and parse one Drive document without mutating campaign or publish state."""

    def __init__(
        self,
        *,
        drive_reader: Any = None,
        parser: Any = parse_campaign_preset,
        clock: Any = time.time,
    ) -> None:
        self.drive_reader = drive_reader or GoogleDriveBriefReader()
        self.parser = parser
        self.clock = clock

    def import_url(self, source_url: str) -> CampaignPresetImportResult:
        url = str(source_url or "").strip()
        file_id = extract_file_id(url)
        if not file_id:
            raise CampaignPresetImportError(
                "Enter a Google Docs link or a Google Drive file link (folders are not supported).",
                code="invalid_url",
                http_status=422,
            )
        snapshot = None
        public_error: GoogleDriveError | None = None
        if configured():
            try:
                snapshot = self.drive_reader.fetch(file_id)
            except GoogleDriveError as exc:
                # A public Docs export can still work when OAuth is absent, has the wrong scope,
                # or the configured account has not been granted access. Do not mask rate limits
                # or malformed IDs with a second request.
                message = str(exc)
                lowered = message.casefold()
                if "rate limit" in lowered or "file id is not valid" in lowered:
                    raise CampaignPresetImportError(
                        message,
                        code="drive_rate_limit" if "rate limit" in lowered else "invalid_file_id",
                        http_status=429 if "rate limit" in lowered else 422,
                    ) from exc
            except Exception as exc:
                logger.info(
                    "campaign preset authenticated Drive read failed (%s)", type(exc).__name__
                )
        if snapshot is None:
            source_host = (urlsplit(url).hostname or "").lower()
            public_source_type = (
                "google_docs"
                if source_host in {"docs.google.com", "www.docs.google.com"}
                else "google_drive"
            )

            def public_snapshot(source_type: str) -> DriveBriefSnapshot:
                content, metadata = download_public_text_file(file_id, source_type=source_type)
                return DriveBriefSnapshot.from_content(
                    file_id,
                    content,
                    revision_id=str(metadata.get("version") or ""),
                    modified_time=str(metadata.get("modifiedTime") or ""),
                    mime_type=str(metadata.get("mimeType") or "text/plain"),
                )

            try:
                snapshot = public_snapshot(public_source_type)
            except GoogleDriveError as exc:
                public_error = exc
                # A Drive sharing URL can point to a native Google Doc. Without OAuth we cannot
                # inspect its MIME type first, so try the Docs export endpoint after an ordinary
                # Drive download failure. Do not spend a second request on malformed IDs or rate limits.
                lowered = str(exc).casefold()
                if public_source_type != "google_docs" and not any(
                    marker in lowered for marker in ("rate limit", "file id is not valid")
                ):
                    try:
                        snapshot = public_snapshot("google_docs")
                    except GoogleDriveError as docs_error:
                        if "rate limit" in str(docs_error).casefold():
                            public_error = docs_error
                    except Exception as docs_error:
                        logger.info(
                            "campaign preset public Docs fallback failed (%s)",
                            type(docs_error).__name__,
                        )
            except Exception as exc:
                logger.info("campaign preset public Drive read failed (%s)", type(exc).__name__)
                public_error = GoogleDriveError(
                    "The Google link could not be read. Confirm link sharing is enabled and use the paste fallback if needed."
                )
        if snapshot is None:
            message = str(public_error or "Google Drive could not be read. Use the paste fallback.")
            code = "drive_rate_limit" if "rate limit" in message.casefold() else "drive_access"
            status = 429 if code == "drive_rate_limit" else 422
            raise CampaignPresetImportError(
                message, code=code, http_status=status
            ) from public_error

        content = str(getattr(snapshot, "content", "") or "")
        if not content.strip():
            raise CampaignPresetImportError(
                "The Google document is empty or has no readable text. Use the paste fallback.",
                code="empty_document",
                http_status=422,
            )
        try:
            preset = self.parser(content)
            if not isinstance(preset, CampaignPreset):
                preset = CampaignPreset.from_dict(preset if isinstance(preset, dict) else {})
        except Exception as exc:
            logger.info("campaign preset parsing failed (%s)", type(exc).__name__)
            raise CampaignPresetImportError(
                "The Google document could not be parsed as a campaign brief. Use the paste "
                "fallback and check the brief format.",
                code="parse_error",
                http_status=422,
            ) from exc

        host = (urlsplit(url).hostname or "").lower()
        source_type = (
            "google_docs" if host in {"docs.google.com", "www.docs.google.com"} else "google_drive"
        )
        revision = str(getattr(snapshot, "revision_id", "") or "")
        content_hash = str(getattr(snapshot, "content_hash", "") or "")
        return CampaignPresetImportResult(
            preset=preset,
            source_url=url,
            source_file_id=file_id,
            source_type=source_type,
            sync_status="synced",
            revision_id=revision,
            content_hash=content_hash,
            modified_time=str(getattr(snapshot, "modified_time", "") or ""),
            mime_type=str(getattr(snapshot, "mime_type", "") or ""),
            parser="deterministic",
            synced_at=float(self.clock()),
        )


__all__ = [
    "CampaignPresetDriveService",
    "CampaignPresetImportError",
    "CampaignPresetImportResult",
]
