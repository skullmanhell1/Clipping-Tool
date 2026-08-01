import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api.js";
import PublishingPanel from "./PublishingPanel.jsx";

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
  tiktok: { configured: true, direct_publish: true, message: "token valid" },
  youtube: { configured: true, direct_publish: false, message: "quota limited" },
  instagram: { configured: false, direct_publish: false, message: "no credentials" },
};

const CAMPAIGNS = [
  { id: "c1", name: "Launch week", routes: { tiktok: {}, youtube: {} } },
  { id: "c2", name: "Evergreen", routes: {} },
];

function setup(props = {}) {
  const onChange = vi.fn();
  const onCampaignSaved = vi.fn();
  const utils = render(
    <PublishingPanel
      value={VALUE}
      statuses={STATUSES}
      campaigns={CAMPAIGNS}
      onChange={onChange}
      onCampaignSaved={onCampaignSaved}
      {...props}
    />,
  );
  return { onChange, onCampaignSaved, ...utils };
}

const open = async () => {
  await userEvent.click(screen.getByRole("button", { name: /Publishing settings/ }));
};

beforeEach(() => {
  vi.spyOn(api, "saveCampaign").mockResolvedValue({ id: "new", name: "Saved" });
});
afterEach(() => vi.restoreAllMocks());

describe("the collapsed panel", () => {
  it("starts closed, because it is secondary to the main settings", () => {
    setup();
    expect(screen.queryByText("Publish To")).toBeNull();
  });

  it("opens and closes on the header", async () => {
    setup();
    await open();
    expect(screen.getByText("Publish To")).toBeInTheDocument();
    await open();
    expect(screen.queryByText("Publish To")).toBeNull();
  });
});

describe("platform selection", () => {
  it("shows each platform's readiness, distinguishing limited from unconfigured", async () => {
    // Three states, not two: "configured but cannot direct-publish" still needs a human, which is
    // different from having no credentials at all.
    setup();
    await open();
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("Limited / review")).toBeInTheDocument();
    expect(screen.getByText("Not configured")).toBeInTheDocument();
  });

  it("shows the backend's message for each platform", async () => {
    setup();
    await open();
    expect(screen.getByText("quota limited")).toBeInTheDocument();
    expect(screen.getByText("no credentials")).toBeInTheDocument();
  });

  it("uses friendly labels rather than raw ids", async () => {
    setup();
    await open();
    expect(screen.getByText("TikTok")).toBeInTheDocument();
    expect(screen.getByText("YouTube")).toBeInTheDocument();
  });

  it("falls back to the raw id for a platform it has no label for", async () => {
    // A publisher added on the backend must still be selectable before the frontend knows its
    // pretty name.
    setup({ statuses: { mastodon: { configured: true, direct_publish: true, message: "" } } });
    await open();
    expect(screen.getByText("mastodon")).toBeInTheDocument();
  });

  it("cannot select an unconfigured platform", async () => {
    // Selecting it would queue publishes that can only fail.
    setup();
    await open();
    expect(screen.getByRole("checkbox", { name: /instagram/i })).toBeDisabled();
  });

  it("adds a platform on check and removes it on uncheck", async () => {
    const { onChange } = setup();
    await open();
    await userEvent.click(screen.getByRole("checkbox", { name: /tiktok/i }));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ platforms: ["tiktok"] }));

    onChange.mockClear();
    const { onChange: onChange2 } = setup({
      value: { ...VALUE, platforms: ["tiktok", "youtube"] },
    });
    await userEvent.click(screen.getAllByRole("button", { name: /Publishing settings/ })[1]);
    await userEvent.click(screen.getAllByRole("checkbox", { name: /tiktok/i })[1]);
    expect(onChange2).toHaveBeenCalledWith(expect.objectContaining({ platforms: ["youtube"] }));
  });

  it("says it is loading when statuses have not arrived", async () => {
    setup({ statuses: null });
    await open();
    expect(screen.getByText("Loading platform status…")).toBeInTheDocument();
  });
});

describe("campaigns", () => {
  it("adopts a campaign's routes as the platform selection", async () => {
    // The whole point of a campaign is that it carries the routing, so picking one must replace
    // the manual selection rather than sit alongside it.
    const { onChange } = setup();
    await open();
    await userEvent.selectOptions(screen.getByLabelText("Campaign"), "c1");
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ campaign_id: "c1", platforms: ["tiktok", "youtube"] }),
    );
  });

  it("keeps the current platforms when the selection is cleared", async () => {
    // "None / manual route" means "I am routing this by hand" — wiping the selection would
    // discard the user's work.
    const { onChange } = setup({ value: { ...VALUE, platforms: ["tiktok"], campaign_id: "c1" } });
    await open();
    await userEvent.selectOptions(screen.getByLabelText("Campaign"), "");
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ campaign_id: "", platforms: ["tiktok"] }),
    );
  });

  it("treats a campaign with no routes as selecting nothing", async () => {
    const { onChange } = setup({ value: { ...VALUE, platforms: ["tiktok"] } });
    await open();
    await userEvent.selectOptions(screen.getByLabelText("Campaign"), "c2");
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ platforms: [] }));
  });
});

describe("saving a campaign", () => {
  it("refuses without a name, and says why", async () => {
    setup({ value: { ...VALUE, platforms: ["tiktok"] } });
    await open();
    await userEvent.click(screen.getByRole("button", { name: /Save routing/ }));
    expect(api.saveCampaign).not.toHaveBeenCalled();
    expect(
      screen.getByText(/Enter a campaign name and select at least one platform/),
    ).toBeVisible();
  });

  it("refuses without a platform", async () => {
    setup();
    await open();
    await userEvent.type(screen.getByPlaceholderText("New campaign name"), "Launch");
    await userEvent.click(screen.getByRole("button", { name: /Save routing/ }));
    expect(api.saveCampaign).not.toHaveBeenCalled();
  });

  it("builds one route per selected platform", async () => {
    setup({
      value: { ...VALUE, platforms: ["tiktok", "youtube"], account_id: "acct", target_id: "t1" },
    });
    await open();
    await userEvent.type(screen.getByPlaceholderText("New campaign name"), "Launch");
    await userEvent.click(screen.getByRole("button", { name: /Save routing/ }));
    await waitFor(() => expect(api.saveCampaign).toHaveBeenCalled());
    expect(api.saveCampaign).toHaveBeenCalledWith("Launch", {
      tiktok: { account_id: "acct", target_type: "", target_id: "t1" },
      youtube: { account_id: "acct", target_type: "", target_id: "t1" },
    });
  });

  it("sends target_type only for Whop, which is the only platform that has one", async () => {
    setup({
      value: { ...VALUE, platforms: ["whop", "tiktok"], target_type: "chat", target_id: "c" },
    });
    await open();
    await userEvent.type(screen.getByPlaceholderText("New campaign name"), "Launch");
    await userEvent.click(screen.getByRole("button", { name: /Save routing/ }));
    await waitFor(() => expect(api.saveCampaign).toHaveBeenCalled());
    const routes = api.saveCampaign.mock.calls[0][1];
    expect(routes.whop.target_type).toBe("chat");
    expect(routes.tiktok.target_type).toBe("");
  });

  it("trims the name", async () => {
    setup({ value: { ...VALUE, platforms: ["tiktok"] } });
    await open();
    await userEvent.type(screen.getByPlaceholderText("New campaign name"), "  Launch  ");
    await userEvent.click(screen.getByRole("button", { name: /Save routing/ }));
    await waitFor(() => expect(api.saveCampaign).toHaveBeenCalledWith("Launch", expect.anything()));
  });

  it("selects the new campaign, tells the parent, and clears the box", async () => {
    const { onChange, onCampaignSaved } = setup({ value: { ...VALUE, platforms: ["tiktok"] } });
    await open();
    const box = screen.getByPlaceholderText("New campaign name");
    await userEvent.type(box, "Launch");
    await userEvent.click(screen.getByRole("button", { name: /Save routing/ }));
    await waitFor(() => expect(onCampaignSaved).toHaveBeenCalledWith({ id: "new", name: "Saved" }));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ campaign_id: "new" }));
    expect(box).toHaveValue("");
  });

  it("survives a parent that passes no onCampaignSaved", async () => {
    setup({ value: { ...VALUE, platforms: ["tiktok"] }, onCampaignSaved: undefined });
    await open();
    await userEvent.type(screen.getByPlaceholderText("New campaign name"), "Launch");
    await userEvent.click(screen.getByRole("button", { name: /Save routing/ }));
    await waitFor(() => expect(api.saveCampaign).toHaveBeenCalled());
  });

  it("shows the failure and stays usable", async () => {
    api.saveCampaign.mockRejectedValue(new Error("duplicate name"));
    setup({ value: { ...VALUE, platforms: ["tiktok"] } });
    await open();
    await userEvent.type(screen.getByPlaceholderText("New campaign name"), "Launch");
    const button = screen.getByRole("button", { name: /Save routing/ });
    await userEvent.click(button);
    expect(await screen.findByText("duplicate name")).toBeInTheDocument();
    expect(button).toBeEnabled();
  });
});

describe("mode, schedule and routing fields", () => {
  it("defaults to review, so nothing publishes without being asked", async () => {
    setup();
    await open();
    expect(screen.getByLabelText("Mode")).toHaveValue("review");
  });

  it("reports a switch to auto publish", async () => {
    const { onChange } = setup();
    await open();
    await userEvent.selectOptions(screen.getByLabelText("Mode"), "auto");
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ mode: "auto" }));
  });

  it("takes the schedule as a local datetime, and says so", async () => {
    // The conversion to epoch happens in the settings schema; the note is the only place the
    // user learns their local time is being converted.
    setup();
    await open();
    expect(screen.getByLabelText("Schedule")).toHaveAttribute("type", "datetime-local");
    expect(screen.getByText(/entered in your browser’s local time/)).toBeInTheDocument();
  });

  it("reports account, target type and target id changes", async () => {
    const { onChange } = setup();
    await open();
    await userEvent.type(screen.getByLabelText(/Account \/ channel/), "acct");
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ account_id: "a" }));

    onChange.mockClear();
    await userEvent.selectOptions(screen.getByLabelText(/Whop target type/), "course");
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ target_type: "course" }));

    onChange.mockClear();
    await userEvent.type(screen.getByLabelText("Target ID"), "x");
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ target_id: "x" }));
  });
});
