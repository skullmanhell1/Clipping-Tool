import { useRef, useState } from "react";

/**
 * Input area: a URL bar (supports multiple URLs, one per line or comma/space
 * separated) plus a file picker for single or batch uploads.
 *
 * Calls onChange({ urls: string[], files: File[] }) whenever the inputs change.
 */
export default function InputBar({ onChange, onPreview }) {
  const [urlText, setUrlText] = useState("");
  const [files, setFiles] = useState([]);
  const fileRef = useRef(null);

  const parseUrls = (text) =>
    text
      .split(/[\s,]+/)
      .map((s) => s.trim())
      .filter(Boolean);

  const emit = (text, fileList) => {
    const urls = parseUrls(text);
    onChange({ urls, files: fileList });
  };

  const handleUrlChange = (e) => {
    setUrlText(e.target.value);
    emit(e.target.value, files);
  };

  const handleFiles = (e) => {
    const list = Array.from(e.target.files || []);
    setFiles(list);
    emit(urlText, list);
  };

  const clearFiles = () => {
    setFiles([]);
    if (fileRef.current) fileRef.current.value = "";
    emit(urlText, []);
  };

  const urls = parseUrls(urlText);

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/50 p-5">
      <div className="flex flex-col gap-3 sm:flex-row">
        {/*
          A textarea, not `<input type="text">`, and the reason is the placeholder.

          The HTML value-sanitisation algorithm for text inputs *strips* CR and LF, so pasting a
          newline-separated list of URLs into one produced a single concatenated string --
          "https://a/1https://a/2" -- which parsed as one unusable URL and was then submitted.
          The control could not honour the behaviour it advertised, and the failure was silent up
          to the point the backend rejected the address.

          Kept to one visible row so the layout is unchanged for the common single-URL case;
          `resize-y` lets someone pasting a column of twenty expand it.
        */}
        <textarea
          rows={1}
          value={urlText}
          onChange={handleUrlChange}
          onBlur={() => urls.length === 1 && onPreview?.(urls[0])}
          placeholder="Paste a video URL (or several, separated by space / new lines)"
          className="flex-1 resize-y rounded-xl border border-slate-700 bg-slate-950 px-4 py-3 text-slate-100 placeholder-slate-500 outline-none transition focus:border-brand-accent"
        />
        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          className="rounded-xl border border-slate-700 bg-slate-800 px-5 py-3 text-sm font-medium text-slate-200 transition hover:border-slate-500"
        >
          Upload file(s)
        </button>
        <input
          ref={fileRef}
          type="file"
          accept="video/*"
          multiple
          onChange={handleFiles}
          className="hidden"
        />
      </div>

      {files.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-2 text-sm text-slate-300">
          <span className="text-slate-500">Selected:</span>
          {files.map((f) => (
            <span key={f.name} className="rounded-lg bg-slate-800 px-2 py-1 text-xs">
              {f.name}
            </span>
          ))}
          <button onClick={clearFiles} className="text-xs text-rose-400 hover:underline">
            clear
          </button>
        </div>
      )}

      {urls.length > 1 && (
        <p className="mt-3 text-xs text-slate-500">
          Batch mode: {urls.length} URLs will be processed in line.
        </p>
      )}
    </div>
  );
}
