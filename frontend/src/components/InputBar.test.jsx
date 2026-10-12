import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import InputBar from "./InputBar.jsx";

const videoFile = (name = "interview.mp4") =>
  new File(["video bytes"], name, { type: "video/mp4", lastModified: 123 });

describe("video source intake", () => {
  it("parses multiple links and previews a single link on request", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const onPreview = vi.fn();
    render(<InputBar onChange={onChange} onPreview={onPreview} />);

    await user.type(
      screen.getByLabelText("Video link"),
      "https://video.example/one{enter}https://video.example/two",
    );

    expect(onChange).toHaveBeenLastCalledWith({
      urls: ["https://video.example/one", "https://video.example/two"],
      files: [],
    });
    expect(screen.getByText("Batch import · 2 links")).toBeInTheDocument();

    await user.clear(screen.getByLabelText("Video links"));
    await user.type(screen.getByLabelText("Video link"), "https://video.example/one");
    await user.click(screen.getByRole("button", { name: "Preview link" }));
    expect(onPreview).toHaveBeenCalledWith("https://video.example/one");
  });

  it("accepts videos from the picker, formats their size, and supports removal", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<InputBar onChange={onChange} />);
    const file = videoFile();

    await user.upload(screen.getByLabelText("Choose video files"), file);

    expect(onChange).toHaveBeenLastCalledWith({ urls: [], files: [file] });
    expect(screen.getByText("interview.mp4")).toBeInTheDocument();
    expect(screen.getByText("1 video · 11 B")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Remove interview.mp4" }));
    expect(onChange).toHaveBeenLastCalledWith({ urls: [], files: [] });
    expect(screen.queryByText("interview.mp4")).not.toBeInTheDocument();
  });

  it("accepts videos by drag and drop and explains skipped non-video files", () => {
    const onChange = vi.fn();
    render(<InputBar onChange={onChange} />);
    const dropzone = screen.getByRole("group", { name: "Video upload dropzone" });
    const accepted = videoFile("recording.mov");
    const rejected = new File(["not a video"], "notes.txt", { type: "text/plain" });

    fireEvent.dragEnter(dropzone);
    expect(screen.getByText("Drop videos to add them")).toBeInTheDocument();
    fireEvent.drop(dropzone, { dataTransfer: { files: [accepted, rejected] } });

    expect(onChange).toHaveBeenLastCalledWith({ urls: [], files: [accepted] });
    expect(screen.getByRole("alert")).toHaveTextContent("1 file was skipped");
  });

  it("keeps pasted links and uploaded files together as one source set", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<InputBar onChange={onChange} />);
    const file = videoFile();

    await user.type(screen.getByLabelText("Video link"), "https://video.example/one");
    await user.upload(screen.getByLabelText("Choose video files"), file);

    expect(onChange).toHaveBeenLastCalledWith({
      urls: ["https://video.example/one"],
      files: [file],
    });
    expect(screen.getByText("2 sources ready")).toBeInTheDocument();
  });
});
