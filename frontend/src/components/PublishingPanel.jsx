import { useState } from "react";
import { api } from "../api.js";

const LABELS = {
  whop: "Whop",
  youtube: "YouTube",
  tiktok: "TikTok",
  instagram: "Instagram",
  x: "X",
};

const inputClass =
  "mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 p-2 text-slate-100 outline-none focus:border-brand-accent";

export default function PublishingPanel({
  value,
  onChange,
  statuses,
  campaigns,
  onCampaignSaved,
  campaignProfile = "",
}) {
  const [show, setShow] = useState(false);
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [verifyingYouTube, setVerifyingYouTube] = useState(false);
  const [youtubeVerification, setYouTubeVerification] = useState(null);

  const toggle = (platform) => {
    const platforms = value.platforms.includes(platform)
      ? value.platforms.filter((item) => item !== platform)
      : [...value.platforms, platform];
    onChange({ ...value, platforms });
  };

  const selectCampaign = (campaignId) => {
    const campaign = campaigns.find((item) => item.id === campaignId);
    onChange({
      ...value,
      campaign_id: campaignId,
      platforms: campaign ? Object.keys(campaign.routes || {}) : value.platforms,
    });
  };

  const saveCampaign = async () => {
    setError("");
    if (!name.trim() || !value.platforms.length) {
      setError("Enter a campaign name and select at least one platform.");
      return;
    }
    setSaving(true);
    try {
      const routes = {};
      value.platforms.forEach((platform) => {
        routes[platform] = {
          account_id: value.account_id,
          target_type: platform === "whop" ? value.target_type : "",
          target_id: value.target_id,
        };
      });
      const campaign = await api.saveCampaign(name.trim(), routes);
      onCampaignSaved?.(campaign);
      onChange({ ...value, campaign_id: campaign.id });
      setName("");
    } catch (saveError) {
      setError(saveError.message || "Campaign save failed.");
    } finally {
      setSaving(false);
    }
  };

  const verifyYouTube = async () => {
    setVerifyingYouTube(true);
    setYouTubeVerification(null);
    setError("");
    try {
      setYouTubeVerification(await api.verifyPublisherCredentials("youtube"));
    } catch (verificationError) {
      setYouTubeVerification({
        verified: false,
        message: verificationError.message || "YouTube credential verification failed.",
      });
    } finally {
      setVerifyingYouTube(false);
    }
  };

  const statusEntries = Object.entries(statuses || {});
  const isBoxablCampaign = campaignProfile === "boxabl_campaign";

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/50 p-5">
      <button
        type="button"
        className="flex w-full items-center justify-between"
        onClick={() => setShow((current) => !current)}
      >
        <span className="text-sm font-semibold uppercase tracking-wider text-slate-400">
          Publishing settings
        </span>
        <span className="text-brand-accent">{show ? "▾" : "▸"}</span>
      </button>

      {show && (
        <div className="mt-4 space-y-4">
          <div>
            <div className="mb-2 text-xs text-slate-500">Publish To</div>
            {statusEntries.length === 0 ? (
              <div className="rounded-lg border border-slate-800 p-3 text-xs text-slate-500">
                Loading platform status…
              </div>
            ) : (
              <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
                {statusEntries.map(([platform, status]) => (
                  <div
                    key={platform}
                    className={`rounded-lg border p-2 text-xs ${
                      status.configured
                        ? "border-slate-700"
                        : "border-slate-800 opacity-60"
                    }`}
                  >
                    <label className="flex items-center gap-2">
                      <input
                        type="checkbox"
                        checked={value.platforms.includes(platform)}
                        onChange={() => toggle(platform)}
                        disabled={!status.configured}
                      />
                      <b>{LABELS[platform] || platform}</b>
                    </label>
                    <div
                      className={
                        status.configured ? "text-emerald-400" : "text-amber-400"
                      }
                    >
                      {status.configured
                        ? status.direct_publish
                          ? "Ready"
                          : "Limited / review"
                        : "Not configured"}
                    </div>
                    <div className="mt-1 text-slate-500">{status.message}</div>
                    {platform === "youtube" ? (
                      <div className="mt-2">
                        <button
                          type="button"
                          onClick={verifyYouTube}
                          disabled={verifyingYouTube || !status.configured}
                          className="rounded border border-red-400/30 px-2 py-1 text-[10px] text-red-200 hover:bg-red-400/10 disabled:opacity-40"
                        >
                          {verifyingYouTube ? "Verifying…" : "Verify YouTube OAuth"}
                        </button>
                        {youtubeVerification ? (
                          <p
                            className={`mt-1 text-[10px] ${
                              youtubeVerification.verified ? "text-emerald-300" : "text-rose-300"
                            }`}
                          >
                            {youtubeVerification.message}
                          </p>
                        ) : null}
                      </div>
                    ) : null}
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <label className="text-xs text-slate-400">
              Campaign
              <select
                value={value.campaign_id}
                onChange={(event) => selectCampaign(event.target.value)}
                className={inputClass}
              >
                <option value="">None / manual route</option>
                {campaigns.map((campaign) => (
                  <option key={campaign.id} value={campaign.id}>
                    {campaign.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="text-xs text-slate-400">
              Mode
              <select
                value={isBoxablCampaign ? "review" : value.mode}
                onChange={(event) => onChange({ ...value, mode: event.target.value })}
                disabled={isBoxablCampaign}
                className={inputClass}
              >
                <option value="review">
                  {isBoxablCampaign ? "Review / draft (campaign required)" : "Review / draft"}
                </option>
                {!isBoxablCampaign ? <option value="auto">Auto publish</option> : null}
              </select>
            </label>
            <label className="text-xs text-slate-400">
              Schedule
              <input
                type="datetime-local"
                value={value.schedule}
                onChange={(event) =>
                  onChange({ ...value, schedule: event.target.value })
                }
                className={inputClass}
              />
            </label>
            <label className="text-xs text-slate-400">
              Account / channel
              <input
                value={value.account_id}
                onChange={(event) =>
                  onChange({ ...value, account_id: event.target.value })
                }
                placeholder="optional account ID"
                className={inputClass}
              />
            </label>
            <label className="text-xs text-slate-400">
              Whop target type
              <select
                value={value.target_type}
                onChange={(event) =>
                  onChange({ ...value, target_type: event.target.value })
                }
                className={inputClass}
              >
                <option value="">None</option>
                <option value="chat">Chat</option>
                <option value="course">Course</option>
                <option value="forum">Forum</option>
                <option value="product">Product</option>
              </select>
            </label>
            <label className="text-xs text-slate-400 sm:col-span-2">
              Target ID
              <input
                value={value.target_id}
                onChange={(event) =>
                  onChange({ ...value, target_id: event.target.value })
                }
                placeholder="channel, course, forum, product, or account target"
                className={inputClass}
              />
            </label>
          </div>

          {isBoxablCampaign && value.platforms.includes("x") ? (
            <label className="block text-xs text-amber-100">
              Personal, trackable BOXABL affiliate URL for X
              <input
                type="url"
                value={value.affiliate_url || ""}
                onChange={(event) => onChange({ ...value, affiliate_url: event.target.value })}
                placeholder="https://your-trackable-affiliate-link"
                className={inputClass}
                required
              />
              <span className="mt-1 block text-[11px] text-slate-500">
                Use your own link from the affiliate portal. Do not use a generic or invented link.
              </span>
            </label>
          ) : null}

          <div className="flex flex-wrap gap-2">
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="New campaign name"
              className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-brand-accent"
            />
            <button
              type="button"
              onClick={saveCampaign}
              disabled={saving}
              className="rounded-lg bg-slate-700 px-3 py-2 text-sm hover:bg-slate-600 disabled:opacity-50"
            >
              {saving ? "Saving…" : "Save routing as campaign"}
            </button>
          </div>
          {error && <p className="text-xs text-rose-400">{error}</p>}
          <p className="text-xs text-slate-500">
            {isBoxablCampaign
              ? "The BOXABL profile always requires a human-reviewed clip and a review/draft upload. Instagram native account tagging and any final publishing step must be completed in the platform app."
              : "Auto mode publishes completed clips through a saved campaign. Review mode leaves publishing to each clip button."} Schedule times are entered in your browser’s local time and converted to UTC for the server.
          </p>
        </div>
      )}
    </div>
  );
}
