"use client";

import { useState } from "react";
import dynamic from "next/dynamic";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { prefersBundledPdfRenderer } from "@/lib/pdf-host";
import type { FilePreviewSource } from "../previewerFor";

/**
 * PDF preview, in one of two shapes depending on what the host can do.
 *
 * Windows (pywebview on EdgeChromium/WebView2) and every real browser keep the
 * native viewer through an `<iframe>`: it comes with paging, zoom, rotation,
 * printing, text selection and in-document search all working, and costs nothing
 * in bundle size. The two macOS shells cannot do that — the Cocoa pywebview
 * shell is a WKWebView, which has no inline PDF renderer, and the Electron shell
 * leaves `plugins` off so Chromium's `pdfViewerEnabled` stays false — and there
 * the iframe loaded, painted nothing, and left the reader with a blank pane.
 * Only those hosts get the app's own renderer. See `@/lib/pdf-host` for how a
 * host is recognised, and why it is not sniffed off `window.pywebview`.
 */
export default function PdfPreview({
  url,
  filename,
}: {
  url: string;
  filename: FilePreviewSource["filename"];
}) {
  // A lazy initialiser, so the user agent is read once per mount rather than on
  // every render of a pane that re-renders on scroll. Deliberately not an
  // effect: deciding after mount would paint the iframe branch first on macOS —
  // the blank pane — for a frame before switching. Both branches are
  // client-only (`FilePreviewContent` mounts this with `ssr: false`), so there
  // is no server markup for the choice to disagree with.
  const [bundled] = useState(prefersBundledPdfRenderer);

  if (!bundled) {
    return <NativePdfPreview url={url} filename={filename} />;
  }
  return <BundledPdfPreview url={url} filename={filename} />;
}

/**
 * The app's own renderer, loaded only where it is needed.
 *
 * `next/dynamic` keeps its ~580 lines out of the chunk the Windows shell and
 * browsers download; they never render this branch, so they never fetch it.
 * `ssr: false` for the same reason as above — it is a canvas, there is nothing
 * to server-render.
 */
const BundledPdfPreview = dynamic(() => import("./BundledPdfPreview"), {
  ssr: false,
  loading: () => (
    <div className="flex h-full w-full items-center justify-center bg-[var(--secondary)]">
      <Loader2 size={14} className="animate-spin text-[var(--muted-foreground)]" />
    </div>
  ),
});

/**
 * The host's own PDF viewer, in an iframe.
 *
 * Unchanged from when it was the only branch: same markup, same loading
 * overlay, same behaviour. The `load` event sometimes fires twice in WebKit
 * because of the inline UA toolbar; we only flip the loading flag on the first
 * one.
 */
function NativePdfPreview({
  url,
  filename,
}: {
  url: string;
  filename: FilePreviewSource["filename"];
}) {
  const { t } = useTranslation();
  const [loading, setLoading] = useState(true);

  return (
    <div className="relative h-full w-full bg-[var(--muted)]/30">
      {loading && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center">
          <div className="flex items-center gap-2 text-[12px] text-[var(--muted-foreground)]">
            <Loader2 size={14} className="animate-spin" />
            <span>{t("Loading preview…")}</span>
          </div>
        </div>
      )}
      <iframe
        title={filename}
        src={url}
        className="h-full w-full border-0 bg-[var(--background)]"
        onLoad={() => setLoading(false)}
      />
    </div>
  );
}
