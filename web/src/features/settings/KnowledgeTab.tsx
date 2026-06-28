import type { MutableRefObject } from "react";
import type { RagDocument } from "@/types";
import { Section } from "./SettingsSection";

export interface KnowledgeTabProps {
  documents: RagDocument[];
  documentsError: string | null;
  documentsBusy: boolean;
  documentFileRef: MutableRefObject<HTMLInputElement | null>;
  onUploadDocument: (file: File) => void;
  onDeleteDocument: (documentId: string) => void;
  onGoToMemory: () => void;
}

/**
 * The "Knowledge" settings tab: RAG document upload/list plus a pointer to
 * the dedicated Memory tab. Extracted from SettingsDrawer (phase 4c).
 */
export function KnowledgeTab({
  documents,
  documentsError,
  documentsBusy,
  documentFileRef,
  onUploadDocument,
  onDeleteDocument,
  onGoToMemory,
}: KnowledgeTabProps) {
  return (
    <>
      <Section title="Documents (RAG)">
        <p className="text-[11px] text-ink-100/50">
          Drop in notes, docs, or PDFs and Aiko will be able to pull relevant
          chunks into the conversation. Indexed into the same retrieval
          substrate as chat history and memories.
        </p>
        {documentsError ? (
          <div className="rounded-md border border-rose-400/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
            {documentsError}
          </div>
        ) : null}
        <div className="flex items-center gap-2">
          <input
            ref={documentFileRef}
            type="file"
            accept=".md,.markdown,.txt,.pdf"
            disabled={documentsBusy}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) onUploadDocument(f);
            }}
            className="block w-full text-xs text-ink-100/70 file:mr-3 file:rounded file:border file:border-white/10 file:bg-white/5 file:px-2 file:py-1 file:text-xs file:text-ink-100"
          />
        </div>
        {documents.length === 0 ? (
          <p className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-2 text-xs text-ink-100/50">
            No documents uploaded yet.
          </p>
        ) : (
          <ul className="space-y-1.5">
            {documents.map((doc) => (
              <li
                key={doc.document_id}
                className="flex items-start justify-between gap-2 rounded-md border border-white/5 bg-white/[0.03] px-3 py-2 text-xs text-ink-100/80"
              >
                <div className="min-w-0 flex-1">
                  <p className="truncate font-medium">{doc.title}</p>
                  <div className="mt-1 flex items-center gap-2 text-[10px] uppercase tracking-wide text-ink-100/40">
                    <span>{doc.chunk_count} chunks</span>
                    <span className="font-mono">{doc.document_id}</span>
                  </div>
                </div>
                <button
                  type="button"
                  disabled={documentsBusy}
                  onClick={() => onDeleteDocument(doc.document_id)}
                  className="shrink-0 rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-rose-400/60 hover:text-rose-200"
                  aria-label={`Remove document ${doc.title}`}
                >
                  remove
                </button>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="Long-term memories">
        <p className="text-[11px] text-ink-100/50">
          Memories live in their own tab. Switch to{" "}
          <button
            type="button"
            onClick={onGoToMemory}
            className="underline decoration-dotted underline-offset-2 hover:text-ink-100"
          >
            Memory
          </button>{" "}
          to inspect, edit, pin, or add memories.
        </p>
      </Section>
    </>
  );
}
