"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  ChevronLeft,
  ChevronRight,
  Download,
  Loader2,
  Maximize2,
  RotateCw,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  loadPdfjs,
  outputScale,
  pdfjsCMapUrl,
  pdfjsWasmUrl,
  type PdfDocument,
  type PdfPageProxy,
} from "@/lib/pdfjs-loader";
import {
  clampPdfZoom,
  canvasPixelRatio,
  fittedPageWidth,
  MAX_PDF_ZOOM,
  MIN_PDF_ZOOM,
  steppedPdfZoom,
} from "@/lib/reading-zoom";
import type { FilePreviewSource } from "../previewerFor";

/**
 * The PDF renderer for hosts that have no PDF plugin: it draws the pages itself,
 * one canvas at a time.
 *
 * Split into its own module and imported through `next/dynamic` by
 * `./PdfPreview`, so the Windows shell and every browser — which keep the native
 * iframe viewer — never download this file at all. `loadPdfjs()` keeps pdf.js
 * itself (1.5 MB with its worker) behind a dynamic import on top of that, so a
 * macOS reader who never opens a PDF pays nothing either.
 */

/**
 * Every page is laid out at its real height from the start, so the scrollbar is
 * honest on a 98-page textbook, but only the pages around the viewport hold a
 * canvas — the rest stay empty placeholders — or the document would ask for
 * memory proportional to its length and blank out halfway down.
 */

/** Pages kept holding a canvas on each side of the viewport. */
const RENDER_MARGIN = 1;
/** Horizontal padding of the page column, in px (`px-4` on the scroll root). */
const COLUMN_INSET = 32;
/** Gap between pages, in px. */
const PAGE_GAP = 12;
/** A4's ratio, used until page one reports the document's own. */
const FALLBACK_RATIO = 1.414;
/** Shared empty map, so a document that has reported nothing allocates nothing. */
const NO_RATIOS: Readonly<Record<number, number>> = {};

/**
 * What to show under "This document could not be opened.".
 *
 * Read here rather than at the catch site so the load effect does not depend on
 * `t`: react-i18next hands back a new `t` on every language change, and a `t` in
 * that dependency list re-downloads the whole PDF just to re-render a string that
 * only ever appears when the download fails.
 */
function errorMessage(caught: unknown): string {
  if (caught instanceof Error) return caught.message;
  if (typeof caught === "string") return caught;
  return "";
}

/**
 * Whether pdf.js should send the session cookie with the file request.
 *
 * The app's own file routes are relative and cookie-gated, so they need it, and
 * pdf.js maps the flag straight to `credentials: "include"`.
 *
 * A third-party `http(s)` URL must not: `resolveSourceUrl` passes such sources
 * through untouched, `include` demands `Access-Control-Allow-Credentials` from
 * the other side, and a presigned CDN link answers `*` or nothing — the fetch
 * then fails on the one host that uses this renderer, where the native viewer on
 * Windows would have shown the file.
 */
function needsCredentials(url: string): boolean {
  // Relative and root-relative ("/api/files/…") are this app's own; so is a bare
  // path with a query. Only an absolute or protocol-relative URL can leave it.
  if (!/^(?:[a-z][a-z\d+\-.]*:)?\/\//i.test(url)) return true;
  if (typeof window === "undefined") return false;
  try {
    return new URL(url, window.location.href).origin === window.location.origin;
  } catch {
    // Unparsable: treat it as foreign rather than attaching the cookie to it.
    return false;
  }
}

type PreviewLoad =
  | { kind: "loading" }
  | { kind: "ready"; doc: PdfDocument; pageCount: number; ratio: number }
  | { kind: "error"; message: string };

interface TaggedLoad {
  /**
   * Which file and which attempt this state describes.
   *
   * Derived rather than reset in an effect: a stale document or a stale error
   * therefore can never be shown for the file now on screen, and a failed load
   * is retried by changing the token instead of by mutating the state machine.
   */
  token: string;
  load: PreviewLoad;
}

function BundledPdfPreview({
  url,
  filename,
}: {
  url: string;
  filename: FilePreviewSource["filename"];
}) {
  const { t } = useTranslation();
  const scrollRef = useRef<HTMLDivElement | null>(null);
  /** Bumped by "Retry"; part of the load token, so it re-runs the loader. */
  const [attempt, setAttempt] = useState(0);
  const [tagged, setTagged] = useState<TaggedLoad>({
    token: "",
    load: { kind: "loading" },
  });
  const [available, setAvailable] = useState(0);
  /** Multiple of the fitted width: 1 means "as wide as the pane allows". */
  const [zoom, setZoom] = useState(1);
  /**
   * The page on screen, tagged with the load it belongs to.
   *
   * Derived from the token rather than reset by an effect: a new file (or a
   * retry) therefore opens on its first page without a second render pass, and a
   * stale measurement can never report a page of the document that is gone.
   */
  const [visible, setVisible] = useState<{ token: string; page: number }>({
    token: "",
    page: 1,
  });
  /**
   * Aspect ratio of the pages that have actually been parsed, by page number.
   *
   * A document's pages are not always the same size — a textbook whose cover or
   * fold-out is taller than the text block is common — and laying every page out
   * at page one's ratio made those pages *taller than their placeholder*, so the
   * canvas was clipped by `overflow-hidden` and the reader saw half a page. That
   * is content missing, not a scrollbar rounding error.
   *
   * Tagged with the load it belongs to for the same reason as `visible`: a new
   * file or a retry starts with no measurements instead of inheriting the ones
   * taken for the file before it.
   */
  const [ratios, setRatios] = useState<{
    token: string;
    byPage: Readonly<Record<number, number>>;
  }>({ token: "", byPage: NO_RATIOS });
  /**
   * Where the reader was looking when the page changed size, so a zoom keeps
   * that point in place instead of snapping the column back to page one.
   */
  const zoomAnchorRef = useRef<{ page: number; offset: number } | null>(null);

  const token = `${attempt}|${url}`;
  const current: PreviewLoad = tagged.token === token ? tagged.load : { kind: "loading" };
  const ready = current.kind === "ready";
  const doc = current.kind === "ready" ? current.doc : null;
  const pageCount = current.kind === "ready" ? current.pageCount : 0;
  const ratio = current.kind === "ready" ? current.ratio : FALLBACK_RATIO;
  const visiblePage = visible.token === token ? visible.page : 1;
  const pageRatios = ratios.token === token ? ratios.byPage : NO_RATIOS;

  // -- document ------------------------------------------------------------

  useEffect(() => {
    let cancelled = false;
    // The loading task — not the document proxy — owns `destroy()`, and
    // destroying it is what releases the worker port and the buffered file.
    // Without it every opened file leaks a port for the life of the session.
    let task: { destroy: () => Promise<void> } | null = null;

    void (async () => {
      try {
        const pdfjs = await loadPdfjs();
        if (cancelled) return;
        const loadingTask = pdfjs.getDocument({
          url,
          // CID fonts (a Chinese textbook is nearly always Adobe-GB1) are
          // translated through pdf.js' predefined character maps. Without them
          // the font is dropped and its runs vanish from the canvas entirely —
          // a page of blank paper rather than a warning.
          cMapUrl: pdfjsCMapUrl(),
          cMapPacked: true,
          // JPEG2000/JBIG2 scan pages are decoded by the wasm modules; textbook
          // scans use both.
          wasmUrl: pdfjsWasmUrl(),
          // The file route sits behind the session cookie like every other API
          // route, and pdf.js does its own fetching, so it needs telling. Only
          // for our own routes — see `needsCredentials`.
          withCredentials: needsCredentials(url),
          // pdf.js asks for 64 KB ranges by default; behind the app's proxy each
          // one is a full request and a browser runs six at a time, so a few-MB
          // textbook queued dozens before its first page could draw.
          rangeChunkSize: 1 << 20,
        });
        task = loadingTask;
        const opened = await loadingTask.promise;
        if (cancelled) return;
        const first = await opened.getPage(1);
        if (cancelled) return;
        const viewport = first.getViewport({ scale: 1 });
        setTagged({
          token,
          load: {
            kind: "ready",
            doc: opened,
            pageCount: opened.numPages,
            ratio: viewport.width > 0 ? viewport.height / viewport.width : FALLBACK_RATIO,
          },
        });
      } catch (caught) {
        if (cancelled) return;
        setTagged({ token, load: { kind: "error", message: errorMessage(caught) } });
      }
    })();

    return () => {
      cancelled = true;
      void task?.destroy();
    };
    // No `t` here on purpose: see `errorMessage`.
  }, [token, url]);

  // -- width ---------------------------------------------------------------

  // The pane's width is what "fit" means here, so it has to be measured rather
  // than derived from a CSS percentage: the pages need a pixel width before a
  // canvas can be sized.
  useLayoutEffect(() => {
    const root = scrollRef.current;
    if (!root) return;
    const measure = () => {
      const width = Math.max(0, root.clientWidth - COLUMN_INSET);
      setAvailable((previous) => (previous === width ? previous : width));
    };
    measure();
    if (typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver(measure);
    observer.observe(root);
    return () => observer.disconnect();
  }, []);

  const pageWidth = useMemo(() => {
    if (!available) return 0;
    return Math.round(fittedPageWidth(available) * clampPdfZoom(zoom));
  }, [available, zoom]);

  /** Height of one page: its own measured ratio once known, page one's until then. */
  const heightOf = (pageNumber: number) =>
    pageWidth > 0
      ? Math.round(pageWidth * (pageRatios[pageNumber] ?? ratio))
      : 0;

  /**
   * Record the ratio a page reported when it was parsed.
   *
   * Only pages that have been in the render window report, so an unmeasured page
   * still lays out at page one's ratio — it is off-screen, and it corrects itself
   * the moment it comes into view. Measuring every page up front would mean
   * parsing the whole file just to lay it out.
   */
  const reportRatio = (pageNumber: number, pageRatio: number) => {
    if (!(pageRatio > 0)) return;
    setRatios((previous) => {
      const sameDocument = previous.token === token;
      const known = sameDocument ? previous.byPage[pageNumber] : undefined;
      // Same answer as last time: no state, no render. Without this a page that
      // re-renders on every zoom would write the same ratio back each time.
      if (known !== undefined && Math.abs(known - pageRatio) < 1e-3) return previous;
      return {
        token,
        byPage: { ...(sameDocument ? previous.byPage : NO_RATIOS), [pageNumber]: pageRatio },
      };
    });
  };

  // A new file starts at its first page. The page counter itself is derived (see
  // `visible` above); this only moves the scroll box back to the top.
  useLayoutEffect(() => {
    const root = scrollRef.current;
    if (root) root.scrollTop = 0;
  }, [doc]);

  // Restore the anchored point once the new page heights have committed.
  useLayoutEffect(() => {
    const anchor = zoomAnchorRef.current;
    zoomAnchorRef.current = null;
    const root = scrollRef.current;
    if (!root || !anchor) return;
    const page = root.querySelector<HTMLElement>(`[data-pdf-page="${anchor.page}"]`);
    if (!page) return;
    const offset = page.getBoundingClientRect().top - root.getBoundingClientRect().top;
    root.scrollTop = Math.max(0, root.scrollTop + offset - anchor.offset);
  }, [pageWidth]);

  // -- which page is on screen --------------------------------------------

  /**
   * Track the page on screen from scroll position, measured directly.
   *
   * Deliberately not an `IntersectionObserver`, for the same reason the reader
   * avoids it: it does not fire at all in some embedded browsers, and there it
   * silently froze the render window at page one — scrolling to page 30 showed
   * an empty placeholder forever. Rect maths on a throttled scroll event asks
   * the question directly ("which page covers most of the viewport") and has no
   * such dependency.
   */
  useEffect(() => {
    const root = scrollRef.current;
    if (!root || !ready) return;

    let frame = 0;
    const measure = () => {
      frame = 0;
      const rootRect = root.getBoundingClientRect();
      if (rootRect.height <= 0) return;
      let best: { page: number; covered: number } | null = null;
      const pages = root.querySelectorAll<HTMLElement>("[data-pdf-page]");
      for (const page of pages) {
        const rect = page.getBoundingClientRect();
        const covered =
          Math.min(rect.bottom, rootRect.bottom) - Math.max(rect.top, rootRect.top);
        if (covered <= 0) continue;
        const number = Number(page.dataset.pdfPage ?? "0");
        if (!(number > 0)) continue;
        if (!best || covered > best.covered) best = { page: number, covered };
      }
      if (!best) return;
      const found = best.page;
      setVisible((previous) =>
        previous.token === token && previous.page === found
          ? previous
          : { token, page: found },
      );
    };

    const onScroll = () => {
      // One measurement per frame: a fast flick fires scroll dozens of times and
      // each measurement walks every page element.
      if (!frame) frame = window.requestAnimationFrame(measure);
    };

    measure();
    root.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      root.removeEventListener("scroll", onScroll);
      if (frame) window.cancelAnimationFrame(frame);
    };
  }, [ready, pageWidth, token]);

  // -- zoom / paging -------------------------------------------------------

  // Plain functions rather than `useCallback`: the React Compiler memoises them
  // itself, and these read state the compiler tracks for it.
  const applyZoom = (next: number) => {
    const root = scrollRef.current;
    const anchorPage = root?.querySelector<HTMLElement>(
      `[data-pdf-page="${visiblePage}"]`,
    );
    if (root && anchorPage) {
      zoomAnchorRef.current = {
        page: visiblePage,
        offset: anchorPage.getBoundingClientRect().top - root.getBoundingClientRect().top,
      };
    }
    setZoom(clampPdfZoom(next));
  };

  const goToPage = (target: number) => {
    const root = scrollRef.current;
    if (!root) return;
    const number = Math.min(Math.max(1, target), Math.max(1, pageCount));
    const page = root.querySelector<HTMLElement>(`[data-pdf-page="${number}"]`);
    if (!page) return;
    // Measured, not `offsetTop`: the page's offset parent is not this scroll
    // container, so `offsetTop` is off by whatever chrome sits above it.
    // Assigned rather than animated — programmatic smooth scrolling is
    // silently a no-op in some embedded browsers.
    const top =
      page.getBoundingClientRect().top -
      root.getBoundingClientRect().top +
      root.scrollTop -
      PAGE_GAP;
    root.scrollTop = Math.max(0, top);
    setVisible({ token, page: number });
  };

  const canZoomOut = zoom > MIN_PDF_ZOOM;
  const canZoomIn = zoom < MAX_PDF_ZOOM;

  // Ctrl/Cmd+wheel zoom is deliberately not bound: inside a long document the
  // trackpad's two-finger scroll and a modifier-scroll arrive as the same kind
  // of event, so the pane would zoom every time a reader flicked sideways. The
  // toolbar's own controls are the only zoom, which also keeps the shortcuts of
  // the chat pane around it (browser zoom, text-size) untouched.

  // -- render --------------------------------------------------------------

  return (
    <div className="relative flex h-full w-full flex-col bg-[var(--secondary)]">
      {current.kind === "ready" && (
        <div className="flex shrink-0 items-center gap-1 border-b border-[var(--border)] bg-[var(--card)] px-2 py-1">
          <button
            type="button"
            aria-label={t("Previous page")}
            title={t("Previous page")}
            disabled={visiblePage <= 1}
            onClick={() => goToPage(visiblePage - 1)}
            className="inline-flex h-6 w-6 items-center justify-center rounded text-[var(--muted-foreground)] transition-colors hover:bg-[var(--muted)] hover:text-[var(--foreground)] disabled:cursor-not-allowed disabled:opacity-40"
          >
            <ChevronLeft size={14} />
          </button>
          <button
            type="button"
            aria-label={t("Next page")}
            title={t("Next page")}
            disabled={visiblePage >= current.pageCount}
            onClick={() => goToPage(visiblePage + 1)}
            className="inline-flex h-6 w-6 items-center justify-center rounded text-[var(--muted-foreground)] transition-colors hover:bg-[var(--muted)] hover:text-[var(--foreground)] disabled:cursor-not-allowed disabled:opacity-40"
          >
            <ChevronRight size={14} />
          </button>
          <span className="px-1 text-[11px] tabular-nums text-[var(--muted-foreground)]">
            {t("Page {{page}} of {{pages}}", {
              page: Math.min(visiblePage, current.pageCount),
              pages: current.pageCount,
            })}
          </span>
          <span className="flex-1" />
          <button
            type="button"
            aria-label={t("Zoom out")}
            title={t("Zoom out")}
            disabled={!canZoomOut}
            onClick={() => applyZoom(steppedPdfZoom(zoom, -1))}
            className="inline-flex h-6 w-6 items-center justify-center rounded text-[var(--muted-foreground)] transition-colors hover:bg-[var(--muted)] hover:text-[var(--foreground)] disabled:cursor-not-allowed disabled:opacity-40"
          >
            <ZoomOut size={14} />
          </button>
          <button
            type="button"
            aria-label={t("Zoom in")}
            title={t("Zoom in")}
            disabled={!canZoomIn}
            onClick={() => applyZoom(steppedPdfZoom(zoom, 1))}
            className="inline-flex h-6 w-6 items-center justify-center rounded text-[var(--muted-foreground)] transition-colors hover:bg-[var(--muted)] hover:text-[var(--foreground)] disabled:cursor-not-allowed disabled:opacity-40"
          >
            <ZoomIn size={14} />
          </button>
          <button
            type="button"
            aria-label={t("Fit width")}
            title={t("Fit width")}
            disabled={zoom === 1}
            onClick={() => applyZoom(1)}
            className="inline-flex h-6 items-center gap-1 rounded px-1.5 text-[11px] font-medium text-[var(--muted-foreground)] transition-colors hover:bg-[var(--muted)] hover:text-[var(--foreground)] disabled:cursor-not-allowed disabled:opacity-40"
          >
            <Maximize2 size={13} />
            {t("Fit width")}
          </button>
        </div>
      )}

      <div
        ref={scrollRef}
        data-pdf-preview-scroll=""
        // `overflow-auto`, not `overflow-y-auto`: a zoomed page can be wider than
        // the pane, and a container that only scrolls vertically would clip it
        // with no way to reach the right-hand columns.
        //
        // The desk is `--secondary`, one step back from the paper. Not
        // `bg-[var(--muted)]/30` — a var() colour has no channels for the `/NN`
        // modifier to reach into, so that rule is dropped and the desk renders
        // the same colour as the page on it.
        className="dt-preview-scroll relative flex-1 overflow-auto overscroll-contain px-4 py-4"
      >
        {doc && pageWidth > 0 && (
          // No `items-center`: a page wider than the pane would overflow on both
          // sides and the left half would be unreachable. Each page carries
          // `mx-auto`, whose auto margins collapse to zero once the free space is
          // negative, so the page starts at the left edge instead.
          <div className="flex flex-col" style={{ gap: PAGE_GAP }}>
            {Array.from({ length: pageCount }, (_, index) => index + 1).map(
              (number) => {
                const active = Math.abs(number - visiblePage) <= RENDER_MARGIN;
                return (
                  <div
                    key={number}
                    data-pdf-page={number}
                    className="relative mx-auto overflow-hidden bg-[var(--background)] shadow-sm"
                    // The page's own ratio when a measurement has landed, page
                    // one's otherwise. `overflow-hidden` then only trims the
                    // sub-pixel difference between this rounded box and the
                    // canvas, instead of cutting a taller page in half.
                    style={{ width: pageWidth, height: heightOf(number) }}
                  >
                    {active && (
                      <PdfPageCanvas
                        doc={doc}
                        pageNumber={number}
                        width={pageWidth}
                        onRatio={reportRatio}
                      />
                    )}
                  </div>
                );
              },
            )}
          </div>
        )}
      </div>

      {current.kind === "loading" && (
        <div className="absolute inset-0 flex items-center justify-center gap-2 bg-[var(--background)] text-[12px] text-[var(--muted-foreground)]">
          <Loader2 size={14} className="animate-spin" />
          <span>{t("Loading preview…")}</span>
        </div>
      )}

      {current.kind === "error" && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-[var(--background)] px-8 text-center">
          <p className="text-[13px] font-medium text-[var(--foreground)]">
            {t("This document could not be opened.")}
          </p>
          {current.message && (
            <p className="max-w-[420px] break-words text-[12px] text-[var(--muted-foreground)]">
              {current.message}
            </p>
          )}
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setAttempt((value) => value + 1)}
              className="inline-flex items-center gap-2 rounded-lg border border-[var(--border)] bg-[var(--card)] px-3 py-1.5 text-[12px] font-medium text-[var(--muted-foreground)] transition-colors hover:bg-[var(--muted)] hover:text-[var(--foreground)]"
            >
              <RotateCw size={13} strokeWidth={1.7} />
              {t("Retry")}
            </button>
            <a
              href={url}
              download={filename}
              className="inline-flex items-center gap-2 rounded-lg border border-[var(--border)] bg-[var(--card)] px-3 py-1.5 text-[12px] font-medium text-[var(--muted-foreground)] transition-colors hover:bg-[var(--muted)] hover:text-[var(--foreground)]"
            >
              <Download size={13} strokeWidth={1.7} />
              {t("Download")}
            </a>
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * One page's canvas.
 *
 * It owns its own render task so that scrolling a page out of the window, or
 * switching files, cancels the draw in flight and frees the bitmap. A detached
 * canvas keeps its device-pixel buffer, and a textbook scrolled end to end would
 * otherwise hold every page it ever showed.
 */
function PdfPageCanvas({
  doc,
  pageNumber,
  width,
  onRatio,
}: {
  doc: PdfDocument;
  pageNumber: number;
  width: number;
  /** Reports this page's real aspect ratio, so its placeholder can match. */
  onRatio: (pageNumber: number, ratio: number) => void;
}) {
  const { t } = useTranslation();
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  /**
   * The draw that failed, as `<page>|<width>`.
   *
   * A key rather than a boolean so the next zoom or page change re-runs the
   * draw by itself — and so the reset needs no state write inside the effect,
   * which would cascade a second render for nothing.
   */
  const drawKey = `${pageNumber}|${width}`;
  const [failedKey, setFailedKey] = useState<string | null>(null);
  const failed = failedKey === drawKey;
  /**
   * This page's proxy, so it can be handed back when the canvas goes away.
   *
   * pdf.js keeps a page's operator list and decoded images on the proxy until
   * `cleanup()` is called, and the document outlives this canvas: without this,
   * a 98-page scan scrolled end to end would leave the whole file parsed in
   * memory — the cost the windowed layout exists to avoid.
   */
  const pageProxyRef = useRef<PdfPageProxy | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    let cancelled = false;
    let task: { cancel: () => void; promise: Promise<void> } | null = null;

    void (async () => {
      try {
        const page = await doc.getPage(pageNumber);
        if (cancelled) return;
        pageProxyRef.current = page;
        const base = page.getViewport({ scale: 1 });
        if (!(base.width > 0)) return;
        // Height and width are reported together: a page taller than the one the
        // column was laid out from would otherwise be clipped to its placeholder.
        onRatio(pageNumber, base.height / base.width);
        const viewport = page.getViewport({ scale: width / base.width });
        // `outputScale()` keeps the bitmap crisp and capped at 2× so a phone's
        // 3× screen cannot ask for a canvas the browser drops; `canvasPixelRatio`
        // then holds it inside the ~16.7M device-pixel budget too, because a tall
        // page zoomed in reaches that on its own and the browser answers with a
        // blank page instead of an error.
        const dpr = canvasPixelRatio(width, viewport.height, outputScale());
        canvas.width = Math.max(1, Math.floor(width * dpr));
        canvas.height = Math.max(1, Math.floor(viewport.height * dpr));
        canvas.style.width = `${width}px`;
        canvas.style.height = `${viewport.height}px`;
        // `canvas` only, never alongside `canvasContext`: pdf.js documents the
        // context parameter as backwards-compatibility requiring `canvas: null`,
        // and passing both is an unsupported combination.
        const renderTask = page.render({
          canvas,
          viewport,
          transform: dpr === 1 ? undefined : [dpr, 0, 0, dpr, 0, 0],
        });
        task = renderTask;
        await renderTask.promise;
        if (cancelled) return;
        // This draw landed, so a failure recorded for the same key is history.
        setFailedKey((previous) => (previous === null ? previous : null));
      } catch (caught) {
        if (cancelled) return;
        // Scroll-away cancellation rejects the render promise by design; only a
        // real failure should tell the reader the page is broken.
        if (caught instanceof Error && caught.name === "RenderingCancelledException") {
          return;
        }
        setFailedKey(drawKey);
      }
    })();

    return () => {
      cancelled = true;
      try {
        task?.cancel();
      } catch {
        // Already settled.
      }
      canvas.width = 0;
      canvas.height = 0;
    };
    // `onRatio` is left out deliberately: it is a plain function from the parent
    // that only wraps a state setter, but its identity is new on every render —
    // listing it here would cancel and restart this page's draw on every scroll.
    // eslint-disable-next-line react-hooks/exhaustive-deps -- onRatio only wraps a state setter
  }, [doc, pageNumber, width, drawKey]);

  /**
   * Hand the page back when this canvas leaves the render window.
   *
   * Its own effect, keyed on the page rather than on `width`: the draw above
   * re-runs on every zoom step, and releasing there would drop the operator list
   * and the decoded images just to re-parse and re-decode them at the new width.
   * `PdfPage` in `components/reading` releases on the same key for the same
   * reason.
   *
   * By the time this runs the draw has already been cancelled (see the cleanup
   * above), and `cleanup()` defers to the running render task anyway instead of
   * throwing. The document's own `destroy()` is a different thing: that releases
   * the worker when the file changes, and is left where it is.
   */
  useEffect(
    () => () => {
      const page = pageProxyRef.current;
      pageProxyRef.current = null;
      page?.cleanup();
    },
    [doc, pageNumber],
  );

  return (
    <>
      <canvas ref={canvasRef} className="block" />
      {failed && (
        <div className="absolute inset-0 flex items-center justify-center bg-[var(--background)] px-4 text-center text-[11px] text-[var(--muted-foreground)]">
          {t("This page could not be rendered.")}
        </div>
      )}
    </>
  );
}

export default BundledPdfPreview;
