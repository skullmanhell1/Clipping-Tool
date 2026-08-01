"""Adapter tests for each platform publisher with mocked HTTP/subprocess."""

from __future__ import annotations

import json
import types

from config import settings
from publishers.base import PublishRequest, PublishState
from publishers.instagram import InstagramPublisher
from publishers.tiktok import TikTokPublisher
from publishers.whop import WhopPublisher
from publishers.x import XPublisher
from publishers.youtube import YouTubePublisher
from tests.fakes import FakeHTTPClient, FakeResponse


def _request(video_file, **overrides):
    kwargs = dict(
        video_path=video_file,
        title="Great clip",
        description="desc",
        hashtags=["#a", "#b"],
        cta="Follow",
        mentions=["@me"],
        mode="auto",
    )
    kwargs.update(overrides)
    return PublishRequest(**kwargs)


# --------------------------------------------------------------------------- #
# YouTube
# --------------------------------------------------------------------------- #
def test_youtube_not_configured(monkeypatch, video_file):
    monkeypatch.setattr(settings, "youtube_client_id", None)
    monkeypatch.setattr(settings, "youtube_refresh_token", None)
    pub = YouTubePublisher(client=FakeHTTPClient(lambda *a: FakeResponse()))
    result = pub.publish(_request(video_file))
    assert result.state == PublishState.FAILED
    assert not pub.status().configured


def test_youtube_public_upload(monkeypatch, video_file):
    monkeypatch.setattr(settings, "youtube_client_id", "cid")
    monkeypatch.setattr(settings, "youtube_client_secret", "secret")
    monkeypatch.setattr(settings, "youtube_refresh_token", "refresh")

    def handler(method, url, kwargs):
        if "oauth2" in url:
            return FakeResponse(json_data={"access_token": "tok"})
        if url.endswith("/videos"):
            return FakeResponse(headers={"location": "https://upload/here"})
        return FakeResponse(json_data={"id": "vid123"})

    pub = YouTubePublisher(client=FakeHTTPClient(handler))
    result = pub.publish(_request(video_file, mode="auto"))
    assert result.success
    assert result.state == PublishState.PUBLISHED
    assert result.external_id == "vid123"
    assert "shorts/vid123" in result.url


def test_youtube_review_is_private(monkeypatch, video_file):
    monkeypatch.setattr(settings, "youtube_client_id", "cid")
    monkeypatch.setattr(settings, "youtube_client_secret", "secret")
    monkeypatch.setattr(settings, "youtube_refresh_token", "refresh")
    captured = {}

    def handler(method, url, kwargs):
        if "oauth2" in url:
            return FakeResponse(json_data={"access_token": "tok"})
        if url.endswith("/videos"):
            captured["privacy"] = kwargs["json"]["status"]["privacyStatus"]
            return FakeResponse(headers={"location": "https://upload/here"})
        return FakeResponse(json_data={"id": "vidPriv"})

    pub = YouTubePublisher(client=FakeHTTPClient(handler))
    result = pub.publish(_request(video_file, mode="review"))
    assert result.state == PublishState.PRIVATE
    assert captured["privacy"] == "private"


# --------------------------------------------------------------------------- #
# TikTok
# --------------------------------------------------------------------------- #
def test_tiktok_draft_when_not_approved(monkeypatch, video_file):
    monkeypatch.setattr(settings, "tiktok_access_token", "tok")
    monkeypatch.setattr(settings, "tiktok_direct_post_approved", False)
    seen = {}

    def handler(method, url, kwargs):
        if "/init/" in url:
            seen["init"] = url
        return FakeResponse(
            json_data={
                "data": {"upload_url": "https://up", "publish_id": "p1"},
                "error": {"code": "ok"},
            }
        )

    pub = TikTokPublisher(client=FakeHTTPClient(handler))
    result = pub.publish(_request(video_file, mode="auto"))
    assert result.state == PublishState.DRAFT
    assert "inbox" in seen["init"]  # uses inbox endpoint, not direct post


def test_tiktok_direct_post_when_approved(monkeypatch, video_file):
    monkeypatch.setattr(settings, "tiktok_access_token", "tok")
    monkeypatch.setattr(settings, "tiktok_direct_post_approved", True)
    seen = {}

    def handler(method, url, kwargs):
        if "/init/" in url:
            seen["init"] = url
        return FakeResponse(
            json_data={
                "data": {"upload_url": "https://up", "publish_id": "p2"},
                "error": {"code": "ok"},
            }
        )

    pub = TikTokPublisher(client=FakeHTTPClient(handler))
    result = pub.publish(_request(video_file, mode="auto"))
    assert result.state == PublishState.PUBLISHED
    assert "inbox" not in seen["init"]


def test_tiktok_api_error_surfaced(monkeypatch, video_file):
    monkeypatch.setattr(settings, "tiktok_access_token", "tok")

    def handler(method, url, kwargs):
        return FakeResponse(json_data={"error": {"code": "rate_limit", "message": "slow down"}})

    pub = TikTokPublisher(client=FakeHTTPClient(handler))
    result = pub.publish(_request(video_file))
    assert result.state == PublishState.FAILED
    assert "slow down" in result.error


# --------------------------------------------------------------------------- #
# Instagram
# --------------------------------------------------------------------------- #
def test_instagram_review_required_without_approval(monkeypatch, video_file):
    monkeypatch.setattr(settings, "instagram_access_token", "tok")
    monkeypatch.setattr(settings, "instagram_account_id", "ig1")
    monkeypatch.setattr(settings, "instagram_content_publish_approved", False)
    pub = InstagramPublisher(client=FakeHTTPClient(lambda *a: FakeResponse()), sleep=lambda s: None)
    result = pub.publish(_request(video_file))
    assert result.state == PublishState.REVIEW_REQUIRED


def test_instagram_publish_flow(monkeypatch, video_file):
    monkeypatch.setattr(settings, "instagram_access_token", "tok")
    monkeypatch.setattr(settings, "instagram_account_id", "ig1")
    monkeypatch.setattr(settings, "instagram_content_publish_approved", True)

    def handler(method, url, kwargs):
        if url.endswith("/media") and method == "POST":
            return FakeResponse(json_data={"id": "container1"})
        if "rupload" in url:
            return FakeResponse()
        if method == "GET":
            return FakeResponse(json_data={"status_code": "FINISHED"})
        if url.endswith("/media_publish"):
            return FakeResponse(json_data={"id": "media99"})
        return FakeResponse()

    pub = InstagramPublisher(client=FakeHTTPClient(handler), sleep=lambda s: None)
    result = pub.publish(_request(video_file, mode="auto"))
    assert result.success
    assert result.state == PublishState.PUBLISHED
    assert result.external_id == "media99"


def test_instagram_review_mode_uploads_draft(monkeypatch, video_file):
    monkeypatch.setattr(settings, "instagram_access_token", "tok")
    monkeypatch.setattr(settings, "instagram_account_id", "ig1")
    monkeypatch.setattr(settings, "instagram_content_publish_approved", True)

    def handler(method, url, kwargs):
        if url.endswith("/media") and method == "POST":
            return FakeResponse(json_data={"id": "containerDraft"})
        return FakeResponse()

    pub = InstagramPublisher(client=FakeHTTPClient(handler), sleep=lambda s: None)
    result = pub.publish(_request(video_file, mode="review"))
    assert result.state == PublishState.DRAFT
    assert result.external_id == "containerDraft"


# --------------------------------------------------------------------------- #
# X
# --------------------------------------------------------------------------- #
def test_x_review_required_without_approval(monkeypatch, video_file):
    monkeypatch.setattr(settings, "x_access_token", "tok")
    monkeypatch.setattr(settings, "x_direct_post_approved", False)
    pub = XPublisher(client=FakeHTTPClient(lambda *a: FakeResponse()), sleep=lambda s: None)
    result = pub.publish(_request(video_file))
    assert result.state == PublishState.REVIEW_REQUIRED


def test_x_chunked_upload_and_post(monkeypatch, video_file):
    monkeypatch.setattr(settings, "x_access_token", "tok")
    monkeypatch.setattr(settings, "x_direct_post_approved", True)

    def handler(method, url, kwargs):
        if url.endswith("/initialize"):
            return FakeResponse(json_data={"id": "media7"})
        if url.endswith("/append") or url.endswith("/finalize"):
            return FakeResponse(json_data={})
        if url.endswith("/tweets"):
            return FakeResponse(json_data={"data": {"id": "tweet42"}})
        return FakeResponse()

    client = FakeHTTPClient(handler)
    pub = XPublisher(client=client, sleep=lambda s: None)
    result = pub.publish(_request(video_file, mode="auto"))
    assert result.success
    assert result.state == PublishState.PUBLISHED
    assert result.external_id == "tweet42"
    assert any(c["url"].endswith("/append") for c in client.calls)


# --------------------------------------------------------------------------- #
# Whop (Node @whop/sdk bridge via subprocess)
# --------------------------------------------------------------------------- #
def _stub_node(monkeypatch, whop_mod):
    """Make the Node bridge *look* installed, without needing it.

    `WhopPublisher` probes `shutil.which(settings.whop_node_binary)` before it ever reaches
    `subprocess.run`, so faking `run` alone is not enough: on a host with no `node` on PATH the
    publisher short-circuits to FAILED and the test fails having exercised none of the code it is
    about.

    That is not a hypothetical. These two tests passed only because the machines they happened to
    run on had Node installed for the *frontend* build -- GitHub's runners ship it, and so did this
    sandbox until a shell without nvm on PATH ran the suite. A test whose result depends on an
    unrelated toolchain being present is a test that will one day fail for a reason nobody can
    connect to the change in front of them, and the tempting fix at that point is a skip, which
    would silently stop checking the bridge at all.

    So the probe is stubbed alongside `run`. Whether a missing Node is reported usefully is a
    separate behaviour, covered by `test_whop_reports_a_missing_node_runtime` below.
    """
    monkeypatch.setattr(whop_mod.shutil, "which", lambda _name: "/usr/bin/node")


def test_whop_not_configured(monkeypatch, video_file):
    monkeypatch.setattr(settings, "whop_api_key", None)
    pub = WhopPublisher()
    result = pub.publish(_request(video_file))
    assert result.state == PublishState.FAILED


def test_whop_upload_and_attach(monkeypatch, video_file):
    monkeypatch.setattr(settings, "whop_api_key", "key")
    import publishers.whop as whop_mod

    def fake_run(cmd, **kwargs):
        payload = json.loads(kwargs["input"])
        assert payload["target_type"] == "chat"
        out = json.dumps(
            {"success": True, "file_id": "file_1", "url": "https://whop/file_1", "attached": True}
        )
        return types.SimpleNamespace(stdout=out, stderr="", returncode=0)

    _stub_node(monkeypatch, whop_mod)
    monkeypatch.setattr(whop_mod.subprocess, "run", fake_run)
    pub = WhopPublisher()
    result = pub.publish(_request(video_file, target_type="chat", target_id="ch1"))
    assert result.success
    assert result.state == PublishState.PUBLISHED
    assert result.external_id == "file_1"


def test_whop_upload_without_target_is_review(monkeypatch, video_file):
    monkeypatch.setattr(settings, "whop_api_key", "key")
    import publishers.whop as whop_mod

    def fake_run(cmd, **kwargs):
        out = json.dumps(
            {"success": True, "file_id": "file_2", "url": "https://whop/file_2", "attached": False}
        )
        return types.SimpleNamespace(stdout=out, stderr="", returncode=0)

    _stub_node(monkeypatch, whop_mod)
    monkeypatch.setattr(whop_mod.subprocess, "run", fake_run)
    pub = WhopPublisher()
    result = pub.publish(_request(video_file))
    assert result.success
    assert result.state == PublishState.REVIEW_REQUIRED
    assert result.external_id == "file_2"


def test_whop_reports_a_missing_node_runtime(monkeypatch, video_file):
    """A missing bridge names the fix, rather than surfacing a FileNotFoundError.

    This is the behaviour the two tests above accidentally depended on, now asserted on purpose:
    the publisher checks for the runtime up front so an operator learns what to install, instead of
    reading an OSError from `subprocess` and guessing.
    """
    monkeypatch.setattr(settings, "whop_api_key", "key")
    import publishers.whop as whop_mod

    monkeypatch.setattr(whop_mod.shutil, "which", lambda _name: None)
    result = WhopPublisher().publish(_request(video_file, target_type="chat", target_id="ch1"))

    assert not result.success
    assert result.state == PublishState.FAILED
    assert "INSTALL_WHOP_BRIDGE" in result.error
    assert "WHOP_NODE_BINARY" in result.error


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def test_registry_builds_all_platforms():
    from publishers import build_publishers

    publishers = build_publishers()
    assert set(publishers) == {"whop", "youtube", "tiktok", "instagram", "x"}
    for pub in publishers.values():
        status = pub.status()
        assert status.platform == pub.name
