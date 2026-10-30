# BOXABL campaign setup

This setup uses the supplied **BOXABL Content** source document and **ClipFlow x BOXABL - Clipper Guidelines & Approval Rules**. On first load, the configured campaign preset store migrates the legacy `boxabl_campaign` data into the ordinary saved `BOXABL Casita` preset shown in the unified Campaign panel.

## Campaign workflow

1. Apply **BOXABL campaign** and paste an individual approved YouTube or Drive *file* link from the brief. The API rejects other URL sources for this profile. Local uploads are allowed, but the operator must confirm the file came from an approved source before publishing.
2. The profile keeps the entire landscape source fitted inside the vertical frame (blurred background, no face crop/zoom), uses legible animated captions, adds `BOXABL` to clip metadata and captions, and renders a separate uppercase `BOXABL` title card from **0:05 to 0:08**. It also normalizes common ASR spellings such as “Boxable” in the clip transcript/caption tokens.
3. Review every render in the player. Confirm the whole Casita is visible by 0:02–0:05, the hook/pacing are good, the exact brand is spoken or displayed by 0:05–0:08, the source and captions are correct, and the clip contains none of the prohibited topics/rips in the guide. These are human checks; the tool does not claim to prove visual context or spoken timing automatically. Editing campaign copy or re-rendering invalidates the previous clip approval; review the new version again.
4. Mark the clip **Approve**, complete the campaign checklist, then upload in **review/draft** mode. Auto-publishing is disabled for this profile. Instagram API publishing is blocked for this campaign because its publisher cannot apply the required native `@boxabl` account tag; download the processed clip and publish/tag it manually in Instagram. For X, provide your own trackable BOXABL affiliate URL in Publishing settings.

The campaign allow-list consists of the individual YouTube video links approved in the supplied Google Doc and the Drive file `Elon Musk Clips` (`1OnH2B5N-TaGyHqPdmT0uxdCeA-38FXry`). The official `@BOXABL` channel is a discovery reference only: neither the channel page nor other videos pass the gate unless each video is explicitly approved and added to the allow-list.

## Google Drive file-link import

The assistant's connected Google Drive account is **not** automatically available to this application process. To import a private Drive file link, configure these server-side environment variables:

- `GOOGLE_DRIVE_CLIENT_ID`
- `GOOGLE_DRIVE_CLIENT_SECRET`
- `GOOGLE_DRIVE_REFRESH_TOKEN`

The refresh token must include the `https://www.googleapis.com/auth/drive.readonly` scope, and that Google account must have Viewer access to the file. The app accepts direct file links such as `drive.google.com/file/d/<ID>/view` and `drive.google.com/open?id=<ID>`; Drive folder browsing is not implemented. Downloads stream to disk and obey the app's `MAX_UPLOAD_BYTES` limit.

## YouTube publishing credentials

A YouTube **API key is not enough to upload videos**. The existing YouTube Data API v3 publisher needs OAuth refresh-token credentials:

- `YOUTUBE_CLIENT_ID`
- `YOUTUBE_CLIENT_SECRET`
- `YOUTUBE_REFRESH_TOKEN` with the `https://www.googleapis.com/auth/youtube.upload` scope
- optional `YOUTUBE_CHANNEL_ID`

Enable YouTube Data API v3 in the Google Cloud project, configure the OAuth consent screen, and authorize the Google account that owns the channel. An installed-app OAuth client can obtain an offline token on the operator's local machine with `google-auth-oauthlib`; do not attempt to complete the local-browser callback on the hosted app server. If one refresh token is generated with both the Drive read-only and YouTube upload scopes, it can be used for both token variables; otherwise issue the appropriate token for each service.

## Credential safety

An OAuth client secret was exposed in the supplied screenshot. **Revoke/rotate that client secret in Google Cloud before configuring the application.** The screenshot does not contain a refresh token, so it does not authorize YouTube uploads. Do not paste credentials into chat, browser-side settings, source files, pull requests, or GitHub. Put replacement values only in an ignored local `.env` file or the deployment platform's secret manager; `.env.example` contains placeholders only. If a refresh token is exposed, revoke it too.

Before exposing the API publicly, set a strong `API_AUTH_TOKEN` and replace wildcard `CORS_ORIGINS` with the exact trusted front-end origin(s). Do not run the server with Drive/YouTube credentials configured and no API authentication.

A BOXABL upload made through the configured YouTube publisher uses review mode, which sets YouTube privacy to private; if OAuth is missing, no upload is attempted. Once OAuth values are installed, check `/api/publishers` and complete a test upload in review mode before attempting any direct publish outside the BOXABL campaign profile.
