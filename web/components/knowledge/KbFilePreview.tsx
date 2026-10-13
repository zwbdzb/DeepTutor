"use client";

import Tooltip from "@/shared/ui/Tooltip";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Check,
  Copy,
  Download,
  FileText,
  Maximize2,
  Minimize2,
  PanelLeftClose,
  PanelLeftOpen,
} from "lucide-react";
import { apiUrl } from "@/lib/api";
import { docIconFor, formatBytes } from "@/lib/doc-attachments";
import {
  resolveSourceUrl,
  type FilePreviewSource,
} from "@/components/chat/preview/previewerFor";

import FilePreviewContent from "@/components/chat/preview/FilePreviewContent";

interface KbFilePreviewProps {
  source: FilePreviewSource | null;
  /**
   * Optional slot rendered to the right of the breadcrumb / title — used
   * to surface metadata (e.g. file count, modified date) inline in the
   * preview header, since this component is the right pane of the
   * master-detail and there's no separate header above it.
   */
  metaSuffix?: React.ReactNode;
  /** Current collapsed state of the file list. When provided, the preview
   *  header shows a toggle so the user can claim more width without
   *  reaching for the file list's own toggle button. */
  fileListCollapsed?: boolean;
  onToggleFileList?: () => void;
}

/**
 * Inline file preview pane that fits the /knowledge master-detail layout.
 *
 * Unlike the chat's slide-in drawer, this is just an in-flow panel — header
 * (filename + actions) on top, body (renderer) below. It owns no animation
 * timers and no portal; the parent decides how/when to mount it. That keeps
 * the visual language consistent with the rest of the knowledge surface
 * (tabs, sidebars, plain panels) instead of pulling in a drawer chrome that
 * fights the page's existing master-detail.
 */
export default function KbFilePreview({
  source,
  metaSuffix,
  fileListCollapsed,
  onToggleFileList,
}: KbFilePreviewProps) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  // Master-detail caps the inline pane height; fullscreen lets a tall PDF use
  // the whole window so you're not stuck reading half a page at a time.
  const [fullscreen, setFullscreen] = useState(false);

  useEffect(() => {
    if (!fullscreen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setFullscreen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fullscreen]);

  const previewUrl = useMemo(
    () => (source ? resolveSourceUrl(source, apiUrl) : null),
    [source],
  );
  if (!source) {
    return (
      <div className="flex h-full flex-col">
        {onToggleFileList && (
          <div className="flex items-center justify-end border-b border-[var(--border)] bg-[var(--card)]/40 px-3 py-1.5">
            <Tooltip label={
                fileListCollapsed ? t("Show file list") : t("Hide file list")
              } side="top">
              <button
                type="button"
                onClick={onToggleFileList}
                aria-label={
                  fileListCollapsed ? t("Show file list") : t("Hide file list")
                }
                className="flex h-7 w-7 items-center justify-center rounded-md text-[var(--muted-foreground)] transition-colors hover:bg-[var(--muted)] hover:text-[var(--foreground)]"
              >
                {fileListCollapsed ? (
                  <PanelLeftOpen size={13} strokeWidth={1.7} />
                ) : (
                  <PanelLeftClose size={13} strokeWidth={1.7} />
                )}
              </button>
            </Tooltip>
          </div>
        )}
        <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 py-12 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-[var(--muted)] text-[var(--muted-foreground)]">
            <FileText className="h-5 w-5" />
          </div>
          <div>
            <div className="text-[13px] font-medium text-[var(--foreground)]">
              {t("Select a file to preview")}
            </div>
            <p className="mt-1 max-w-xs text-[11.5px] leading-relaxed text-[var(--muted-foreground)]">
              {t(
                "Pick any document from the list on the left to view it here without leaving the knowledge base.",
              )}
            </p>
          </div>
        </div>
      </div>
    );
  }

  const spec = docIconFor(source.filename);
  const HeaderIcon = spec.Icon;
  const sizeLabel = source.size ? formatBytes(source.size) : "";

  const handleCopy = async () => {
    if (!previewUrl) return;
    try {
      await navigator.clipboard.writeText(previewUrl);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard rejected; ignore
    }
  };

  return (
    <div
      className={
        fullscreen
          ? "fixed inset-0 z-50 flex min-h-0 flex-col bg-[var(--background)]"
          : "mx-auto flex h-full w-full min-h-0 max-w-5xl flex-col"
      }
    >
      {/* Header */}
      <div className="flex items-center gap-2 border-b border-[var(--border)] bg-[var(--card)]/80 px-3 py-2">
        {onToggleFileList && !fullscreen && (
          <Tooltip label={
              fileListCollapsed ? t("Show file list") : t("Hide file list")
            } side="top">
            <button
              type="button"
              onClick={onToggleFileList}
              aria-label={
                fileListCollapsed ? t("Show file list") : t("Hide file list")
              }
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-[var(--muted-foreground)] transition-colors hover:bg-[var(--muted)] hover:text-[var(--foreground)]"
            >
              {fileListCollapsed ? (
                <PanelLeftOpen size={13} strokeWidth={1.7} />
              ) : (
                <PanelLeftClose size={13} strokeWidth={1.7} />
              )}
            </button>
          </Tooltip>
        )}
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-[var(--muted)]/60">
          <HeaderIcon size={15} strokeWidth={1.6} className={spec.tint} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-[12.5px] font-medium text-[var(--foreground)]">
            {source.filename}
          </div>
          <div className="truncate text-[10.5px] uppercase tracking-wide text-[var(--muted-foreground)]">
            {sizeLabel ? `${spec.label} · ${sizeLabel}` : spec.label}
          </div>
        </div>

        {metaSuffix && (
          <div className="shrink-0 text-[11px] text-[var(--muted-foreground)]">
            {metaSuffix}
          </div>
        )}

        <Tooltip label={fullscreen ? t("Exit fullscreen") : t("Fullscreen")} side="top">
          <button
            type="button"
            onClick={() => setFullscreen((value) => !value)}
            aria-label={fullscreen ? t("Exit fullscreen") : t("Fullscreen")}
            className="flex h-7 w-7 items-center justify-center rounded-md text-[var(--muted-foreground)] transition-colors hover:bg-[var(--muted)] hover:text-[var(--foreground)]"
          >
            {fullscreen ? (
              <Minimize2 size={13} strokeWidth={1.7} />
            ) : (
              <Maximize2 size={13} strokeWidth={1.7} />
            )}
          </button>
        </Tooltip>

        {previewUrl && (
          <>
            <Tooltip label={t("Download")} side="top">
              <a
                href={previewUrl}
                download={source.filename}
                aria-label={t("Download")}
                className="flex h-7 w-7 items-center justify-center rounded-md text-[var(--muted-foreground)] transition-colors hover:bg-[var(--muted)] hover:text-[var(--foreground)]"
              >
                <Download size={13} strokeWidth={1.7} />
              </a>
            </Tooltip>
            <Tooltip label={t("Copy link")} side="top">
              <button
                type="button"
                onClick={() => void handleCopy()}
                aria-label={t("Copy link")}
                className="flex h-7 w-7 items-center justify-center rounded-md text-[var(--muted-foreground)] transition-colors hover:bg-[var(--muted)] hover:text-[var(--foreground)]"
              >
                {copied ? (
                  <Check
                    size={13}
                    strokeWidth={1.7}
                    className="text-emerald-500"
                  />
                ) : (
                  <Copy size={13} strokeWidth={1.7} />
                )}
              </button>
            </Tooltip>
          </>
        )}
      </div>

      {/* Body */}
      <div className="relative min-h-0 flex-1 overflow-hidden">
        <FilePreviewContent source={source} />
      </div>
    </div>
  );
}
