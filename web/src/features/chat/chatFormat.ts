import { backendBase } from "@/desktop/runtime";

/**
 * Build the static URL for an uploaded attachment's stored file from its
 * ``Attachments:<uuid><ext>`` rel_path. Used for image thumbnails in the
 * composer tray and on user bubbles.
 */
export function attachmentUrl(relPath: string): string {
  const storedName = relPath.includes(":")
    ? relPath.split(":").slice(1).join(":")
    : relPath;
  return `${backendBase().http}/attachment-files/${encodeURIComponent(storedName)}`;
}

/** Short ``HH:MM`` timestamp for a message bubble footer. */
export function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "";
  }
}
