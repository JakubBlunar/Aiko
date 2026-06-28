import type { AttachmentRef } from "@/types";
import { attachmentUrl } from "./chatFormat";

interface AttachmentTrayProps {
  attachments: AttachmentRef[];
  uploadingCount: number;
  error: string | null;
  onRemove: (ref: AttachmentRef) => void;
}

/** D2 Part B: staged attachments above the composer. Images show a
 * thumbnail; text files show a document chip. Each has a remove ✕. */
export function AttachmentTray({
  attachments,
  uploadingCount,
  error,
  onRemove,
}: AttachmentTrayProps) {
  if (attachments.length === 0 && uploadingCount === 0 && !error) {
    return null;
  }
  return (
    <div className="mb-2 flex flex-wrap items-center gap-2">
      {attachments.map((att) => (
        <div
          key={att.rel_path}
          className="group/att relative flex items-center gap-2 rounded-lg border border-white/10 bg-black/30 py-1 pl-1 pr-2"
          title={att.filename}
        >
          {att.kind === "image" ? (
            <img
              src={attachmentUrl(att.rel_path)}
              alt={att.filename}
              className="h-9 w-9 rounded object-cover"
            />
          ) : (
            <span className="flex h-9 w-9 items-center justify-center rounded bg-white/5 text-base">
              📄
            </span>
          )}
          <span className="max-w-[10rem] truncate text-xs text-ink-100/80">
            {att.filename}
          </span>
          <button
            type="button"
            onClick={() => onRemove(att)}
            className="ml-1 rounded px-1 text-xs text-ink-100/50 hover:bg-white/10 hover:text-ink-100"
            title="Remove attachment"
            aria-label={`Remove ${att.filename}`}
          >
            ✕
          </button>
        </div>
      ))}
      {uploadingCount > 0 ? (
        <span className="rounded-lg border border-white/10 bg-black/30 px-3 py-2 text-xs text-ink-100/60">
          Uploading {uploadingCount}…
        </span>
      ) : null}
      {error ? (
        <span className="rounded-lg border border-red-400/30 bg-red-500/10 px-3 py-2 text-xs text-red-200">
          {error}
        </span>
      ) : null}
    </div>
  );
}
