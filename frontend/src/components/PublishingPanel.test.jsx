import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import PublishingPanel from "./PublishingPanel.jsx";
import { api } from "../api.js";

/**
 * This panel decides where a finished clip is sent and whether it goes out on its own. The mode
 * switch is the consequential control: "auto publish" means completed clips leave the machine
 * without anyone looking at them, and "review / draft" means they wait. Mixing those up is not
 * recoverable — a post is public the moment it lands.
 *
 * The routing fields are the other half. A campaign stores one route per platform, and only Whop
 * takes a `target_type` (a chat, a course, a forum); every other platform gets an empty one, so a
 * route built for five platforms must not stamp "chat" onto YouTube. That mistake shows up as a
 * publish failure days later, in a background worker, with no user watching.
 *
 * The panel is a controlled component with no state of its own for `value`, so most tests render it
 * inside a small stateful harness — the same arrangement App.jsx provides. Typing into a field whose
 * value never comes back would only ever record a single keystroke.
 */

const VALUE = {
  platforms: [],
  campaign_id: "",
  mode: "review",
  schedule: "",
  account_id: "",
  target_type: "",
  target_id: "",
};

const STATUSES = {
  tiktok: { configured: true, direct_publish: true, message: "session token present" },
  instagram: { configured: true, direct_publish: false, message: "needs review approval" },
  whop: { configured: true, direct_publish: true, message: "api key present" },
  youtube: { configured: false, direct_publish: false, message: "no oauth client" },
};

const CAMPAIGNS = [
  { id: "c1", name: "Launch week", routes: { tiktok: {}, instagram: {} } },
  { id: "c2", name: "Evergreen", routes: {} },
];

/** Mirrors App.jsx: the panel is controlled, so the harness holds the value it edits. */
function Harness({ initial, onChange, statuses, campaigns, onCampaignSaved }) {
  const [value, setValue] = useState(initial);
  return (
    <PublishingPanel
      value={value}
      onChange={(next) => {
        setValue(next);
        onChange(next);
      }}
      statuses={statuses}
      campaigns={campaigns}
      onCampaignSaved={onCampaignSaved}
    />
  );
}

/** Render the panel already expanded, which is where every control lives. */
const setup = async (initial = VALUE, props = {}) => {
  const onChange = vi.fn();
  const onCampaignSaved = vi.fn();
  const utils = render(
    <Harness
      initial={{ ...VALUE, ...initial }}
      onChange={onChange}
      statuses={props.statuses === undefined ? STATUSES : props.statuses}
      campaigns={props.campaigns || CAMPAIGNS}
      onCampaignSaved={onCampaignSaved}
    />
  );
  await userEvent.click(screen.getByRole("button", { name: /publishing settings/i }));
  return { ...utils, onChange, onCampaignSaved };
};

const lastValue = (onChange) => onChange.mock.calls.at(-1)[0];

afterEach(() => {
  vi.restoreAllMocks();
});

describe("PublishingPanel disclosure", () => {
  it("starts collapsed", async () => {
    // Fifteen controls above the submit button, most of which are irrelevant to a user who is not
    // publishing from this tool at all.
    const onChange = vi.fn();
    render(
      <Harness
        initial={VALUE}
        onChange={onChange}
        statuses={STATUSES}
        campaigns={CAMPAIGNS}
        onCampaignSaved={vi.fn()}
      />
    );
    expect(screen.queryByText("Publish To")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /publishing settings/i }));
    expect(screen.getByText("Publish To")).toBeInTheDocument();
  });

  it("says the platform list is loading rather than claiming nothing is configured", async () => {
    // An empty grid and "no platforms configured" are indistinguishable to a user, and one of them
    // is a lie while /api/publishers is still in flight.
    await setup(VALUE, { statuses: {} });
    expect(screen.getByText(/loading platform status…/i)).toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("tolerates a missing statuses payload", async () => {
    await setup(VALUE, { statuses: null });
    expect(screen.getByText(/loading platform status…/i)).toBeInTheDocument();
  });
});

describe("PublishingPanel platform selection", () => {
  it("adds a platform without disturbing the rest of the routing", async () => {
    const { onChange } = await setup({ account_id: "acct-9", target_id: "chan-1" });
    await userEvent.click(screen.getByRole("checkbox", { name: /tiktok/i }));
    expect(lastValue(onChange)).toEqual({
      ...VALUE,
      account_id: "acct-9",
      target_id: "chan-1",
      platforms: ["tiktok"],
    });
  });

  it("removes only the platform that was unchecked", async () => {
    // A filter that dropped the wrong entry would silently stop publishing to a platform the user
    // still has ticked on screen.
    const { onChange } = await setup({ platforms: ["tiktok", "instagram", "whop"] });
    await userEvent.click(screen.getByRole("checkbox", { name: /instagram/i }));
    expect(lastValue(onChange).platforms).toEqual(["tiktok", "whop"]);
  });

  it("reflects the platforms already selected", async () => {
    await setup({ platforms: ["instagram"] });
    expect(screen.getByRole("checkbox", { name: /instagram/i })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /tiktok/i })).not.toBeChecked();
  });

  it("cannot select a platform that is not configured", async () => {
    // Selecting one would queue an attempt that fails on credentials, after the render has been
    // paid for.
    await setup();
    expect(screen.getByRole("checkbox", { name: /youtube/i })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: /tiktok/i })).toBeEnabled();
  });

  it("distinguishes ready, review-only and unconfigured platforms", async () => {
    // "Limited / review" is not a warning about setup, it is a statement about what publishing will
    // do: the post will be held rather than go live, and that has to be visible before the run.
    await setup();
    const row = (platform) =>
      screen.getByRole("checkbox", { name: new RegExp(platform, "i") }).closest("label");
    expect(row("tiktok")).toHaveTextContent("Ready");
    expect(row("instagram")).toHaveTextContent("Limited / review");
    expect(row("youtube")).toHaveTextContent("Not configured");
  });

  it("shows each platform's own explanation from the server", async () => {
    await setup();
    expect(screen.getByText("no oauth client")).toBeInTheDocument();
    expect(screen.getByText("needs review approval")).toBeInTheDocument();
  });

  it("falls back to the raw platform id when there is no friendly label", async () => {
    await setup(VALUE, {
      statuses: { mastodon: { configured: true, direct_publish: true, message: "" } },
    });
    expect(screen.getByRole("checkbox", { name: /mastodon/i })).toBeInTheDocument();
  });
});

describe("PublishingPanel campaigns", () => {
  it("adopts the platforms a campaign routes to", async () => {
    // The campaign *is* the routing. Keeping a manually ticked platform that the campaign has no
    // route for would publish to it with no account and no target.
    const { onChange } = await setup({ platforms: ["whop"] });
    await userEvent.selectOptions(screen.getByRole("combobox", { name: /campaign/i }), "c1");
    expect(lastValue(onChange)).toMatchObject({
      campaign_id: "c1",
      platforms: ["tiktok", "instagram"],
    });
  });

  it("selects nothing for a campaign that routes nowhere", async () => {
    const { onChange } = await setup({ platforms: ["whop"] });
    await userEvent.selectOptions(screen.getByRole("combobox", { name: /campaign/i }), "c2");
    expect(lastValue(onChange).platforms).toEqual([]);
  });

  it("keeps the manual selection when the campaign is cleared", async () => {
    // Going back to "manual route" must not also clear the platforms, or the user loses the
    // selection they made before picking a campaign by mistake.
    const { onChange } = await setup({ platforms: ["whop"], campaign_id: "c1" });
    await userEvent.selectOptions(screen.getByRole("combobox", { name: /campaign/i }), "");
    expect(lastValue(onChange)).toMatchObject({ campaign_id: "", platforms: ["whop"] });
  });

  it("lists the saved campaigns plus the manual option", async () => {
    await setup();
    const select = screen.getByRole("combobox", { name: /campaign/i });
    expect([...select.options].map((option) => option.textContent)).toEqual([
      "None / manual route",
      "Launch week",
      "Evergreen",
    ]);
  });
});

describe("PublishingPanel mode and routing fields", () => {
  it("switches to automatic publishing", async () => {
    // This is the control that decides whether a clip goes public unattended.
    const { onChange } = await setup();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: /mode/i }), "auto");
    expect(lastValue(onChange).mode).toBe("auto");
  });

  it("switches back to review", async () => {
    const { onChange } = await setup({ mode: "auto" });
    await userEvent.selectOptions(screen.getByRole("combobox", { name: /mode/i }), "review");
    expect(lastValue(onChange).mode).toBe("review");
  });

  it("offers review and auto, and nothing else", async () => {
    await setup();
    const select = screen.getByRole("combobox", { name: /mode/i });
    expect([...select.options].map((option) => option.value)).toEqual(["review", "auto"]);
  });

  it("propagates the account or channel as typed", async () => {
    const { onChange } = await setup();
    await userEvent.type(screen.getByPlaceholderText(/optional account id/i), "UC123");
    expect(lastValue(onChange).account_id).toBe("UC123");
  });

  it("propagates the target id as typed", async () => {
    const { onChange } = await setup();
    await userEvent.type(screen.getByPlaceholderText(/channel, course, forum/i), "forum-7");
    expect(lastValue(onChange).target_id).toBe("forum-7");
  });

  it("propagates the Whop target type", async () => {
    const { onChange } = await setup();
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: /whop target type/i }),
      "course"
    );
    expect(lastValue(onChange).target_type).toBe("course");
  });

  it("shows the schedule field pre-filled from the current value", async () => {
    // The help text promises the value is read as browser-local time and converted to UTC by the
    // server, so what is displayed has to be exactly what was stored.
    await setup({ schedule: "2024-05-08T09:30" });
    expect(screen.getByLabelText(/schedule/i)).toHaveValue("2024-05-08T09:30");
  });

  it("explains what each mode does and how schedule times are interpreted", async () => {
    await setup();
    expect(screen.getByText(/auto mode publishes completed clips/i)).toBeInTheDocument();
    expect(screen.getByText(/converted to UTC for the server/i)).toBeInTheDocument();
  });
});

describe("PublishingPanel campaign saving", () => {
  it("refuses to save without a name", async () => {
    // A nameless campaign cannot be selected again, so it is saved work that is immediately lost.
    const save = vi.spyOn(api, "saveCampaign");
    await setup({ platforms: ["tiktok"] });
    await userEvent.click(screen.getByRole("button", { name: /save routing as campaign/i }));
    expect(
      screen.getByText(/enter a campaign name and select at least one platform/i)
    ).toBeInTheDocument();
    expect(save).not.toHaveBeenCalled();
  });

  it("refuses to save a campaign that routes nowhere", async () => {
    const save = vi.spyOn(api, "saveCampaign");
    await setup();
    await userEvent.type(screen.getByPlaceholderText(/new campaign name/i), "Launch week");
    await userEvent.click(screen.getByRole("button", { name: /save routing as campaign/i }));
    expect(save).not.toHaveBeenCalled();
  });

  it("gives Whop its target type and every other platform an empty one", async () => {
    // `target_type` names a Whop-specific destination kind. Sending "chat" as TikTok's target type
    // is a route the TikTok publisher cannot interpret, and it would only fail at publish time.
    const save = vi
      .spyOn(api, "saveCampaign")
      .mockResolvedValue({ id: "c9", name: "Launch week", routes: {} });
    await setup({
      platforms: ["whop", "tiktok"],
      account_id: "acct-9",
      target_type: "chat",
      target_id: "chan-1",
    });
    await userEvent.type(screen.getByPlaceholderText(/new campaign name/i), "Launch week");
    await userEvent.click(screen.getByRole("button", { name: /save routing as campaign/i }));
    await waitFor(() =>
      expect(save).toHaveBeenCalledWith("Launch week", {
        whop: { account_id: "acct-9", target_type: "chat", target_id: "chan-1" },
        tiktok: { account_id: "acct-9", target_type: "", target_id: "chan-1" },
      })
    );
  });

  it("trims the campaign name", async () => {
    const save = vi
      .spyOn(api, "saveCampaign")
      .mockResolvedValue({ id: "c9", name: "Launch week", routes: {} });
    await setup({ platforms: ["tiktok"] });
    await userEvent.type(screen.getByPlaceholderText(/new campaign name/i), "  Launch week  ");
    await userEvent.click(screen.getByRole("button", { name: /save routing as campaign/i }));
    await waitFor(() => expect(save).toHaveBeenCalledWith("Launch week", expect.any(Object)));
  });

  it("reports the new campaign upward and selects it", async () => {
    // The parent owns the campaign list; without the callback the campaign the user just saved is
    // absent from the dropdown until the page is reloaded.
    const campaign = { id: "c9", name: "Launch week", routes: { tiktok: {} } };
    vi.spyOn(api, "saveCampaign").mockResolvedValue(campaign);
    const { onChange, onCampaignSaved } = await setup({ platforms: ["tiktok"] });
    await userEvent.type(screen.getByPlaceholderText(/new campaign name/i), "Launch week");
    await userEvent.click(screen.getByRole("button", { name: /save routing as campaign/i }));
    await waitFor(() => expect(onCampaignSaved).toHaveBeenCalledWith(campaign));
    expect(lastValue(onChange).campaign_id).toBe("c9");
  });

  it("clears the name field after saving, so the next save is not a duplicate", async () => {
    vi.spyOn(api, "saveCampaign").mockResolvedValue({ id: "c9", name: "Launch week", routes: {} });
    await setup({ platforms: ["tiktok"] });
    await userEvent.type(screen.getByPlaceholderText(/new campaign name/i), "Launch week");
    await userEvent.click(screen.getByRole("button", { name: /save routing as campaign/i }));
    await waitFor(() => expect(screen.getByPlaceholderText(/new campaign name/i)).toHaveValue(""));
  });

  it("surfaces the server's reason for refusing a campaign", async () => {
    vi.spyOn(api, "saveCampaign").mockRejectedValue(new Error("a campaign by that name exists"));
    await setup({ platforms: ["tiktok"] });
    await userEvent.type(screen.getByPlaceholderText(/new campaign name/i), "Launch week");
    await userEvent.click(screen.getByRole("button", { name: /save routing as campaign/i }));
    expect(await screen.findByText("a campaign by that name exists")).toBeInTheDocument();
  });

  it("keeps the typed name when the save fails", async () => {
    // Clearing it on failure would make the user retype the name to retry.
    vi.spyOn(api, "saveCampaign").mockRejectedValue(new Error("nope"));
    await setup({ platforms: ["tiktok"] });
    await userEvent.type(screen.getByPlaceholderText(/new campaign name/i), "Launch week");
    await userEvent.click(screen.getByRole("button", { name: /save routing as campaign/i }));
    await screen.findByText("nope");
    expect(screen.getByPlaceholderText(/new campaign name/i)).toHaveValue("Launch week");
  });

  it("blocks a second save while the first is in flight", async () => {
    // Two identical POSTs create two campaigns with the same name.
    let release;
    const save = vi.spyOn(api, "saveCampaign").mockReturnValue(
      new Promise((resolve) => {
        release = resolve;
      })
    );
    await setup({ platforms: ["tiktok"] });
    await userEvent.type(screen.getByPlaceholderText(/new campaign name/i), "Launch week");
    await userEvent.click(screen.getByRole("button", { name: /save routing as campaign/i }));

    expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
    release({ id: "c9", name: "Launch week", routes: {} });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /save routing as campaign/i })).toBeEnabled()
    );
    expect(save).toHaveBeenCalledTimes(1);
  });

  it("clears a previous validation error on the next attempt", async () => {
    vi.spyOn(api, "saveCampaign").mockResolvedValue({ id: "c9", name: "Launch week", routes: {} });
    await setup({ platforms: ["tiktok"] });
    await userEvent.click(screen.getByRole("button", { name: /save routing as campaign/i }));
    expect(screen.getByText(/enter a campaign name/i)).toBeInTheDocument();
    await userEvent.type(screen.getByPlaceholderText(/new campaign name/i), "Launch week");
    await userEvent.click(screen.getByRole("button", { name: /save routing as campaign/i }));
    await waitFor(() =>
      expect(screen.queryByText(/enter a campaign name/i)).not.toBeInTheDocument()
    );
  });
});
