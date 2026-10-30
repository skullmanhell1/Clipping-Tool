import { useEffect, useRef, useState } from "react";

const VIDEO_EXTENSION = /\.(mp4|mov|m4v|mkv|webm|avi|mpeg|mpg|3gp|mts|m2ts|flv|ogv)$/i;

const parseUrls = (text) =>
  text
    .split(/[\s,]+/)
    .map((value) => value.trim())
    .filter(Boolean);

const isVideoFile = (file) => file.type.startsWith("video/") || VIDEO_EXTENSION.test(file.name);

const formatBytes = (bytes) => {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / 1024 ** index;
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
};

const sameFile = (left, right) =>
  left.name === right.name && left.size === right.size && left.lastModified === right.lastModified;

/**
 * Source intake for links and local media. Accepts multiple links and files,
 * supports drag-and-drop, and reports every selected source to the parent.
 *
 * Calls onChange({ urls: string[], files: File[] }) whenever the inputs change.
 */
export default function InputBar({ onChange = () => {}, onPreview, prefillUrls = [] }) {
  const [urlText, setUrlText] = useState("");
  const [files, setFiles] = useState([]);
  const [fileError, setFileError] = useState("");
  const [isDragging, setIsDragging] = useState(false);
  const fileRef = useRef(null);
  const previewRef = useRef(null);
  const dragDepthRef = useRef(0);
  const urls = parseUrls(urlText);
  const totalBytes = files.reduce((total, file) => total + file.size, 0);

  useEffect(() => {
    const additions = (prefillUrls || []).filter(Boolean);
    if (!additions.length) return;
    setUrlText((currentText) => {
      const nextUrls = [...new Set([...parseUrls(currentText), ...additions])];
      const nextText = nextUrls.join("\n");
      if (nextText !== currentText) onChange({ urls: nextUrls, files });
      return nextText;
    });
  }, [prefillUrls, files, onChange]);

  useEffect(() => {
    const video = previewRef.current;
    const file = files[0];
    if (!video || !file || !("srcObject" in video)) return undefined;

    // Assign the selected File directly. Avoid turning its bytes into a URL string and feeding
    // that string back into a DOM URL sink; this preview only needs a local media object.
    try {
      video.srcObject = file;
    } catch {
      // Browsers without File support for srcObject still allow selecting and uploading files;
      // the preview is an optional convenience, not a prerequisite for processing.
      return undefined;
    }
    return () => {
      video.srcObject = null;
    };
  }, [files]);

  const emit = (text, selectedFiles) => {
    onChange({ urls: parseUrls(text), files: selectedFiles });
  };

  const handleUrlChange = (event) => {
    const nextText = event.target.value;
    setUrlText(nextText);
    emit(nextText, files);
  };

  const addFiles = (fileList) => {
    const incoming = Array.from(fileList || []);
    if (!incoming.length) return;

    const accepted = incoming.filter(isVideoFile);
    const rejectedCount = incoming.length - accepted.length;
    setFileError(
      rejectedCount
        ? `${rejectedCount} file${rejectedCount === 1 ? " was" : "s were"} skipped. Choose a video file.`
        : ""
    );

    const nextFiles = [...files];
    accepted.forEach((file) => {
      if (!nextFiles.some((existing) => sameFile(existing, file))) nextFiles.push(file);
    });
    if (nextFiles.length !== files.length) {
      setFiles(nextFiles);
      emit(urlText, nextFiles);
    }
  };

  const handleFiles = (event) => {
    addFiles(event.target.files);
    // Let a creator pick the same file again after removing it.
    event.target.value = "";
  };

  const removeFile = (index) => {
    const nextFiles = files.filter((_, fileIndex) => fileIndex !== index);
    setFiles(nextFiles);
    setFileError("");
    emit(urlText, nextFiles);
  };

  const clearFiles = () => {
    setFiles([]);
    setFileError("");
    if (fileRef.current) fileRef.current.value = "";
    emit(urlText, []);
  };

  const handleDragEnter = (event) => {
    event.preventDefault();
    dragDepthRef.current += 1;
    setIsDragging(true);
  };

  const handleDragLeave = (event) => {
    event.preventDefault();
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) setIsDragging(false);
  };

  const handleDrop = (event) => {
    event.preventDefault();
    dragDepthRef.current = 0;
    setIsDragging(false);
    addFiles(event.dataTransfer?.files);
  };

  return (
    <section
      aria-labelledby="source-heading"
      className="overflow-hidden rounded-2xl border border-slate-800 bg-slate-900/70 shadow-xl shadow-black/10"
    >
      <div className="border-b border-slate-800/80 px-5 py-4 sm:px-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 id="source-heading" className="text-base font-semibold text-white">
              Add your video
            </h3>
            <p className="mt-1 text-sm text-slate-400">
              Paste a supported YouTube link, a Google Drive file link, or upload a local video. You can mix sources in one run.
            </p>
          </div>
          <div className="rounded-full border border-slate-700 bg-slate-950/60 px-3 py-1 text-xs font-medium text-slate-300">
            {files.length + urls.length === 0
              ? "MP4, MOV, WebM and more"
              : `${files.length + urls.length} source${files.length + urls.length === 1 ? "" : "s"} ready`}
          </div>
        </div>
      </div>

      <div className="grid gap-5 p-5 sm:p-6 lg:grid-cols-[minmax(0,1.05fr)_minmax(300px,0.95fr)]">
        <div className="space-y-2">
          <label htmlFor="video-url-list" className="block text-sm font-medium text-slate-300">
            Video link{urls.length > 1 ? "s" : ""}
          </label>
          <textarea
            id="video-url-list"
            value={urlText}
            onChange={handleUrlChange}
            placeholder="Paste an approved YouTube video URL or Google Drive file link…"
            rows={5}
            className="w-full resize-y rounded-xl border border-slate-700 bg-slate-950 px-4 py-3 text-sm text-slate-100 placeholder-slate-600 outline-none transition focus:border-violet-400 focus:ring-2 focus:ring-violet-500/20"
          />
          <div className="flex min-h-8 flex-wrap items-center justify-between gap-2 text-xs">
            {urls.length > 1 ? (
              <span className="text-slate-400">Batch import · {urls.length} links</span>
            ) : (
              <span className="text-slate-500">Separate multiple links with a new line or comma.</span>
            )}
            {urls.length === 1 && onPreview ? (
              <button
                type="button"
                onClick={() => onPreview(urls[0])}
                className="rounded-lg px-2 py-1 font-medium text-violet-300 transition hover:bg-violet-400/10 hover:text-violet-200"
              >
                Preview link
              </button>
            ) : null}
          </div>
        </div>

        <div className="space-y-3">
          <div
            role="group"
            aria-label="Video upload dropzone"
            onDragEnter={handleDragEnter}
            onDragOver={(event) => event.preventDefault()}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
            className={`flex min-h-36 flex-col items-center justify-center rounded-2xl border border-dashed px-5 py-6 text-center transition ${
              isDragging
                ? "border-violet-300 bg-violet-400/10"
                : "border-slate-700 bg-slate-950/40 hover:border-slate-500"
            }`}
          >
            <div className="mb-3 flex h-11 w-11 items-center justify-center rounded-2xl bg-violet-400/10 text-xl text-violet-300" aria-hidden="true">
              ⇧
            </div>
            <p className="text-sm font-medium text-slate-200">
              {isDragging ? "Drop videos to add them" : "Drag videos here"}
            </p>
            <p className="mt-1 text-xs text-slate-500">or choose files from your device</p>
            <button
              type="button"
              onClick={() => fileRef.current?.click()}
              className="mt-3 rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-2 text-xs font-semibold text-slate-200 transition hover:border-violet-400/70 hover:bg-slate-800"
            >
              Browse videos
            </button>
            <input
              ref={fileRef}
              type="file"
              accept="video/*,.mkv,.avi,.mpeg,.mpg,.3gp,.mts,.m2ts,.flv,.ogv"
              multiple
              onChange={handleFiles}
              className="hidden"
              aria-label="Choose video files"
            />
          </div>

          {fileError ? (
            <p role="alert" className="text-xs text-rose-300">
              {fileError}
            </p>
          ) : null}

          {files.length > 0 ? (
            <div className="overflow-hidden rounded-xl border border-slate-800 bg-slate-950/50">
              <div className="flex items-center justify-between border-b border-slate-800 px-3 py-2 text-xs">
                <span className="font-medium text-slate-300">
                  {files.length} video{files.length === 1 ? "" : "s"} · {formatBytes(totalBytes)}
                </span>
                <button
                  type="button"
                  onClick={clearFiles}
                  className="rounded px-1.5 py-1 text-slate-500 transition hover:bg-rose-400/10 hover:text-rose-300"
                >
                  Clear all
                </button>
              </div>
              <video
                ref={previewRef}
                controls
                preload="metadata"
                className="aspect-video max-h-52 w-full bg-black object-contain"
                aria-label={`Preview ${files[0].name}`}
              />
              <ul className="max-h-36 divide-y divide-slate-800/80 overflow-y-auto">
                {files.map((file, index) => (
                  <li key={`${file.name}-${file.size}-${file.lastModified}-${index}`} className="flex items-center gap-3 px-3 py-2">
                    <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-slate-800 text-xs text-violet-300" aria-hidden="true">
                      ▶
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-xs font-medium text-slate-200">{file.name}</span>
                      <span className="block text-[11px] text-slate-500">{formatBytes(file.size)}</span>
                    </span>
                    <button
                      type="button"
                      onClick={() => removeFile(index)}
                      aria-label={`Remove ${file.name}`}
                      className="rounded-md px-2 py-1 text-xs text-slate-500 transition hover:bg-rose-400/10 hover:text-rose-300"
                    >
                      Remove
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}
