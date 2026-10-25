"""Multi-source Google Docs/Drive campaign profile importing."""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from config import settings
from worker.campaign_import_cache import CampaignImportCache
from worker.campaign_profile import CampaignProfile, parse_campaign_profile
from worker.google_drive import (
    GoogleDriveError,
    configured,
    download_asset,
    download_public_text_file,
    download_text_file,
    extract_file_id,
    extract_folder_id,
    get_file_metadata,
    list_folder_files,
)

logger = logging.getLogger(__name__)

SUPPORTED_TEXT_MIMES = {
    "application/vnd.google-apps.document",
    "text/plain",
    "text/csv",
    "application/json",
}
ASSET_MIMES = {"image/png", "image/jpeg", "image/svg+xml", "font/ttf", "font/otf"}


class CampaignImportError(RuntimeError):
    def __init__(self, message: str, *, code: str = "import_error", http_status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True)
class ImportedSource:
    source_url: str
    source_type: str
    file_id: str = ""
    name: str = ""
    mime_type: str = ""
    revision_id: str = ""
    modified_time: str = ""
    content_hash: str = ""
    status: str = "synced"
    error: str = ""
    content: str = ""
    assets: tuple[dict[str, Any], ...] = ()

    def provenance(self) -> dict[str, Any]:
        return {
            "source_url": self.source_url,
            "source_type": self.source_type,
            "file_id": self.file_id,
            "name": self.name,
            "mime_type": self.mime_type,
            "revision_id": self.revision_id,
            "modified_time": self.modified_time,
            "content_hash": self.content_hash,
            "status": self.status,
            "error": self.error,
            "assets": list(self.assets),
        }


@dataclass(frozen=True)
class CampaignImportResult:
    profile: CampaignProfile
    sources: tuple[ImportedSource, ...]
    cached: bool = False
    synced_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile.to_dict(),
            "sources": [source.provenance() for source in self.sources],
            "provenance": [source.provenance() for source in self.sources],
            "cached": self.cached,
            "synced_at": self.synced_at,
            "requires_review": bool(
                self.profile.ambiguities
                or self.profile.uncertainties
                or any(source.status == "error" for source in self.sources)
            ),
            "status": "partial" if any(source.status == "error" for source in self.sources) else "synced",
        }


class CampaignImportService:
    def __init__(self, *, cache: CampaignImportCache | None = None, clock: Any = time.time) -> None:
        self.cache = cache or CampaignImportCache()
        self.clock = clock

    def import_urls(self, urls: list[str], *, force: bool = False, download_assets: bool = False) -> CampaignImportResult:
        clean_urls = []
        for value in urls:
            value = str(value or "").strip()
            if value and value not in clean_urls:
                clean_urls.append(value)
        if not clean_urls:
            raise CampaignImportError("Provide at least one Google Docs or Drive link.", code="missing_source", http_status=400)
        sources: list[ImportedSource] = []
        chunks: list[str] = []
        structured: list[dict[str, Any]] = []
        errors: list[CampaignImportError] = []
        for url in clean_urls:
            try:
                items = self._read_url(url, force=force, download_assets=download_assets)
            except CampaignImportError as exc:
                errors.append(exc)
                sources.append(ImportedSource(url, "unknown", status="error", error=str(exc)))
                continue
            for source in items:
                sources.append(source)
                if source.content:
                    chunks.append(source.content)
        if not chunks:
            if errors:
                raise errors[0]
            raise CampaignImportError("No supported readable campaign documents were found in the supplied links.", code="unreadable_format", http_status=422)
        profile = parse_campaign_profile(
            "\n\n".join(chunks),
            structured_blocks=structured,
            provenance=[source.provenance() for source in sources],
        )
        return CampaignImportResult(profile, tuple(sources), cached=all(source.status == "cached" for source in sources), synced_at=float(self.clock()))

    def _read_url(self, url: str, *, force: bool, download_assets: bool) -> list[ImportedSource]:
        file_id = extract_file_id(url)
        folder_id = extract_folder_id(url)
        if not file_id and not folder_id:
            raise CampaignImportError(
                "Unsupported link. Use a Google Docs file link or a Google Drive folder/file link.",
                code="invalid_url",
                http_status=422,
            )
        # `extract_folder_id` accepts Drive's generic `open?id=...` shape for compatibility, but
        # that URL does not identify whether the id is a file or a folder. When the file parser can
        # identify the same id, treat it as a file; otherwise ordinary shared file links would be
        # sent down the folder traversal path and fail with a misleading Viewer-access message.
        if folder_id and not file_id:
            if not configured():
                raise CampaignImportError(
                    "Google Drive folder traversal requires server-side OAuth Viewer access. Use individual public Docs links or configure Drive OAuth.",
                    code="private_no_access",
                    http_status=409,
                )
            try:
                return self._read_folder(folder_id, url, force=force, download_assets=download_assets, seen=set())
            except GoogleDriveError as exc:
                raise self._map_error(exc) from exc
        hostname = (urlsplit(url).hostname or "").lower()
        source_type = "google_docs" if hostname in {"docs.google.com", "www.docs.google.com"} else "google_drive"
        if not configured():
            public_key = self.cache.key(url)
            if not force:
                cached = self.cache.get(public_key)
                if cached and cached.get("content"):
                    return [ImportedSource(url, source_type, file_id, str(cached.get("name") or ""), str(cached.get("mime_type") or "text/plain"), content_hash=str(cached.get("content_hash") or ""), status="cached", content=str(cached["content"]))]
            try:
                content, metadata = download_public_text_file(file_id, source_type=source_type)
            except GoogleDriveError as exc:
                raise self._map_error(exc) from exc
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            self.cache.put(public_key, {"source_url": url, "source_type": source_type, "file_id": file_id, "name": str(metadata.get("name") or ""), "mime_type": str(metadata.get("mimeType") or "text/plain"), "content_hash": content_hash, "content": content, "assets": []})
            return [ImportedSource(url, source_type, file_id, str(metadata.get("name") or ""), str(metadata.get("mimeType") or "text/plain"), content_hash=content_hash, content=content)]
        try:
            metadata = get_file_metadata(file_id, require_video=False)
            mime = str(metadata.get("mimeType") or "")
            if mime in ASSET_MIMES and download_assets:
                asset = download_asset(file_id, Path(settings.campaign_import_cache_dir) / "assets")
                return [ImportedSource(url, "drive_asset", file_id, str(metadata.get("name") or ""), mime, assets=({"path": str(asset), "file_id": file_id, "source_url": url, "mime_type": mime},))]
            return self._read_file(url, file_id, metadata, force=force, download_assets=download_assets)
        except GoogleDriveError as exc:
            if any(token in str(exc).casefold() for token in ("access", "denied", "private", "viewer")):
                try:
                    content, metadata = download_public_text_file(file_id, source_type=source_type)
                    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                    return [ImportedSource(url, source_type, file_id, str(metadata.get("name") or ""), str(metadata.get("mimeType") or "text/plain"), content_hash=content_hash, content=content)]
                except GoogleDriveError:
                    pass
            raise self._map_error(exc) from exc

    def _read_folder(self, folder_id: str, source_url: str, *, force: bool, download_assets: bool, seen: set[str], depth: int = 0) -> list[ImportedSource]:
        if folder_id in seen or depth > 8:
            return []
        seen.add(folder_id)
        rows = list_folder_files(folder_id)
        results: list[ImportedSource] = []
        for row in rows:
            child_id = str(row.get("id") or "")
            mime = str(row.get("mimeType") or "")
            if not child_id:
                continue
            child_url = f"https://drive.google.com/file/d/{child_id}/view"
            if mime == "application/vnd.google-apps.folder":
                results.extend(self._read_folder(child_id, child_url, force=force, download_assets=download_assets, seen=seen, depth=depth + 1))
            elif mime in SUPPORTED_TEXT_MIMES:
                try:
                    results.extend(self._read_file(child_url, child_id, row, force=force, download_assets=download_assets))
                except CampaignImportError as exc:
                    results.append(ImportedSource(child_url, "google_drive", child_id, str(row.get("name") or ""), mime, status="error", error=str(exc)))
            elif download_assets and mime in ASSET_MIMES:
                try:
                    asset = download_asset(child_id, Path(settings.campaign_import_cache_dir) / "assets")
                    results.append(ImportedSource(child_url, "drive_asset", child_id, str(row.get("name") or ""), mime, assets=({"path": str(asset), "file_id": child_id, "source_url": child_url, "mime_type": mime},)))
                except GoogleDriveError as exc:
                    results.append(ImportedSource(child_url, "drive_asset", child_id, str(row.get("name") or ""), mime, status="error", error=str(exc)))
        return results

    def _read_file(self, url: str, file_id: str, metadata: dict[str, Any], *, force: bool, download_assets: bool) -> list[ImportedSource]:
        mime = str(metadata.get("mimeType") or "")
        if mime not in SUPPORTED_TEXT_MIMES:
            return [ImportedSource(url, "google_docs" if mime == "application/vnd.google-apps.document" else "google_drive", file_id, str(metadata.get("name") or ""), mime, status="error", error="Unsupported document format")]
        revision = str(metadata.get("headRevisionId") or metadata.get("version") or metadata.get("modifiedTime") or "")
        cached_key = self.cache.key(url, revision_id=revision)
        if not force:
            cached = self.cache.get(cached_key)
            if cached and cached.get("content"):
                return [ImportedSource(url, str(cached.get("source_type") or "google_drive"), file_id, str(cached.get("name") or ""), mime, revision, str(metadata.get("modifiedTime") or ""), str(cached.get("content_hash") or ""), status="cached", content=str(cached["content"]), assets=tuple(cached.get("assets") or []))]
        try:
            content, metadata = download_text_file(file_id, mime_type=mime)
        except GoogleDriveError as exc:
            raise self._map_error(exc) from exc
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        item = ImportedSource(url, "google_docs" if mime == "application/vnd.google-apps.document" else "google_drive", file_id, str(metadata.get("name") or ""), mime, revision, str(metadata.get("modifiedTime") or ""), content_hash, content=content)
        self.cache.put(cached_key, {"source_url": url, "source_type": item.source_type, "file_id": file_id, "name": item.name, "mime_type": mime, "revision_id": revision, "content_hash": content_hash, "content": content, "assets": []})
        return [item]

    @staticmethod
    def _map_error(exc: GoogleDriveError) -> CampaignImportError:
        message = str(exc)
        folded = message.casefold()
        if "rate limit" in folded or "429" in folded:
            return CampaignImportError(message, code="rate_limited", http_status=429)
        if "private" in folded or "denied" in folded or "access" in folded or "oauth" in folded:
            return CampaignImportError(message, code="private_no_access", http_status=403)
        if "unsupported" in folded or "not a google doc" in folded or "not a text" in folded:
            return CampaignImportError(message, code="unreadable_format", http_status=422)
        return CampaignImportError(message or "Google Drive could not be read.", code="drive_error", http_status=422)


__all__ = ["CampaignImportError", "CampaignImportResult", "CampaignImportService", "ImportedSource"]
