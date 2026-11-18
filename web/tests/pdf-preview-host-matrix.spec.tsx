import { useState } from "react";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { Loader2 } from "lucide-react";
import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { useTranslation } from "react-i18next";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import PdfPreview from "@/components/chat/preview/previewers/PdfPreview";
import { prefersBundledPdfRenderer } from "@/lib/pdf-host";

/**
 * Independent verification of the PDF preview fix, by the QA pass.
 *
 * Deliberately does not import or reuse `pdf-host.spec.ts` /
 * `pdf-preview-canvas.spec.tsx`: those are the implementation's own tests, and
 * the point of this file is to check the same contract from the outside — the
 * component as the shell actually mounts it, the DOM Windows is supposed to keep
 * byte for byte, and the failure paths nobody demoed.
 *
 * Three things are pinned here that the shipped tests do not pin:
 *
 * 1. The dispatch decision is exercised through `navigator.userAgent` and the
 *    real `@/lib/pdf-host` for nine real-world hosts, and every row asserts both
 *    the rendered branch and the pure function that chooses it, so the two
 *    cannot drift apart.
 * 2. The Windows/browser branch is compared by `innerHTML` against a verbatim
 *    copy of the pre-change component (`git show HEAD:…/PdfPreview.tsx`) — the
 *    user's actual requirement is "Windows must not change", and that is a DOM
 *    equality claim, not a "it still has an iframe" claim.
 * 3. Layout is modelled (scroll box, page rects, scroll clamping) so the render
 *    window, the paging boundaries and the zoom anchor can be asserted, rather
 *    than only the initial paint.
 */

const fixture = vi.hoisted(() => ({
  getDocument: vi.fn(),
  /** Counts `loadPdfjs()` calls, to prove the native path never decodes. */
  loadPdfjs: vi.fn(),
  /** Every loading task's `destroy`, in the order the tasks were created. */
  destroys: [] as Array<ReturnType<typeof vi.fn>>,
  /**
   * Every page proxy the component asked for, in order.
   *
   * `getPage(n)` is called again when the page is released, so counts are
   * aggregated per page number rather than per object.
   */
  pages: [] as Array<{
    pageNumber: number;
    cleanup: ReturnType<typeof vi.fn>;
  }>,
}));

/** A loading task whose `destroy` is recorded, so teardown can be counted. */
function loadingTask<T>(promise: Promise<T>) {
  const destroy = vi.fn(async () => undefined);
  fixture.destroys.push(destroy);
  return { destroy, promise };
}

vi.mock("@/lib/pdfjs-loader", () => ({
  outputScale: () => 1,
  pdfjsWasmUrl: () => "http://localhost/pdfjs/wasm/",
  pdfjsCMapUrl: () => "http://localhost/pdfjs/cmaps/",
  loadPdfjs: fixture.loadPdfjs,
}));

vi.mock("react-i18next", () => {
  // Interpolating, and a single stable `t`: a fresh function per render would
  // re-run the loader effect and destroy the task on every keystroke-like
  // re-render, which is both a real hazard and something this suite counts.
  const t = (key: string, options?: Record<string, unknown>) =>
    options
      ? key.replace(/\{\{(\w+)\}\}/g, (_whole, name: string) =>
          String(options[name] ?? ""),
        )
      : key;
  return { useTranslation: () => ({ t }) };
});

// -- the hosts, as they actually report themselves --------------------------

const WKWEBVIEW_MAC =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko)";
const SAFARI_MAC =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15";
const CHROME_MAC =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";
const WEBVIEW2_WINDOWS =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0";
const FIREFOX_MAC =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:127.0) Gecko/20100101 Firefox/127.0";
/** iPadOS in desktop-class mode reports a Mac; it has a real PDF viewer. */
const SAFARI_IPAD =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1";
/** Chromium's UA with the shell's marker appended — the macOS Electron shell. */
const ELECTRON_MAC = `${CHROME_MAC} Electron/45.1.0 OpenDeepTutor/1.9.1 (macOS; arm64)`;
/** The same marker on Windows: WebView2 has a PDF plugin, so it must stay native. */
const ELECTRON_WINDOWS = `${WEBVIEW2_WINDOWS} Electron/45.1.0 OpenDeepTutor/1.9.1 (Windows; x64)`;

const HOSTS: Array<{ name: string; userAgent: string; bundled: boolean }> = [
  { name: "macOS WKWebView shell (pywebview Cocoa)", userAgent: WKWEBVIEW_MAC, bundled: true },
  { name: "Windows WebView2 shell", userAgent: WEBVIEW2_WINDOWS, bundled: false },
  { name: "macOS Electron shell (UA also says Chrome/)", userAgent: ELECTRON_MAC, bundled: true },
  { name: "Windows Electron shell", userAgent: ELECTRON_WINDOWS, bundled: false },
  { name: "Safari on macOS", userAgent: SAFARI_MAC, bundled: false },
  { name: "Chrome on macOS", userAgent: CHROME_MAC, bundled: false },
  { name: "Firefox on macOS", userAgent: FIREFOX_MAC, bundled: false },
  { name: "iPadOS desktop-class Safari", userAgent: SAFARI_IPAD, bundled: false },
  { name: "no user agent at all", userAgent: "", bundled: false },
];

// -- geometry, which jsdom does not compute ---------------------------------

const PDF_PAGE_WIDTH = 600; // pt, as the mocked document reports it
const PDF_PAGE_RATIO = 1.414; // A4
const PANE_WIDTH = 648; // `.dt-preview-scroll` clientWidth
const COLUMN_INSET = 32; // `px-4` on the scroll root
/** `fittedPageWidth(648 - 32)` at zoom 1. */
const PAGE_CSS_WIDTH = 616;
const PAGE_GAP = 12;
const PADDING = 16; // `py-4` on the scroll root
const VIEWPORT_HEIGHT = 800;

function heightOf(element: HTMLElement): number {
  return Number.parseFloat(element.style.height || "0");
}

/** Distance from the top of the page column to this page, gaps included. */
function offsetInColumn(page: HTMLElement): number {
  let top = 0;
  for (
    let sibling = page.previousElementSibling;
    sibling;
    sibling = sibling.previousElementSibling
  ) {
    top += heightOf(sibling as HTMLElement) + PAGE_GAP;
  }
  return top;
}

function contentHeight(root: HTMLElement): number {
  const pages = root.querySelectorAll<HTMLElement>("[data-pdf-page]");
  const last = pages[pages.length - 1];
  if (!last) return VIEWPORT_HEIGHT;
  return PADDING * 2 + offsetInColumn(last) + heightOf(last);
}

/**
 * Model the browser's scroll clamp on the scroll root.
 *
 * jsdom's own `scrollTop` is an inert 0, so a component that assigns it would
 * look like it never scrolled; this makes assignment mean what a browser means.
 */
function installScroller(root: HTMLElement): void {
  let scrollTop = 0;
  Object.defineProperty(root, "scrollTop", {
    configurable: true,
    get: () => scrollTop,
    set: (value: number) => {
      const limit = Math.max(0, contentHeight(root) - VIEWPORT_HEIGHT);
      scrollTop = Math.max(0, Math.min(value, limit));
    },
  });
}

function buildDocument(
  pageCount: number,
  ratios: Record<number, number> = {},
) {
  const getPage = vi.fn(async (pageNumber: number) => {
    const ratio = ratios[pageNumber] ?? PDF_PAGE_RATIO;
    const page = {
      getViewport: ({ scale }: { scale: number }) => ({
        width: PDF_PAGE_WIDTH * scale,
        height: PDF_PAGE_WIDTH * ratio * scale,
      }),
      render: () => ({ promise: Promise.resolve(), cancel: () => undefined }),
      // Real pdf.js page proxies have it, and the component hands the page back
      // through it; without the method the release is a swallowed TypeError,
      // which would make a "it frees the page" assertion pass on nothing.
      cleanup: vi.fn(),
      pageNumber,
    };
    fixture.pages.push(page);
    return page;
  });
  return { numPages: pageCount, getPage };
}

/** How many times page `pageNumber`'s proxy was released. */
function cleanupCallsFor(pageNumber: number): number {
  return fixture.pages
    .filter((page) => page.pageNumber === pageNumber)
    .reduce((total, page) => total + page.cleanup.mock.calls.length, 0);
}

// -- harness ----------------------------------------------------------------

/**
 * Pretend to be a given host.
 *
 * An own accessor on the instance shadows jsdom's `Navigator.prototype` getter;
 * `delete` in the teardown uncovers the original again.
 */
function actAsHost(userAgent: string): void {
  Object.defineProperty(window.navigator, "userAgent", {
    configurable: true,
    get: () => userAgent,
  });
}

let hadOwnUserAgent = false;

/**
 * Warm the lazily-imported renderer once, outside any `waitFor`.
 *
 * `next/dynamic` transforms and loads that module on its first mount, which can
 * outlast a `waitFor` window and would make the first bundled-host row look
 * like a failed branch.
 */
beforeAll(async () => {
  await import("@/components/chat/preview/previewers/BundledPdfPreview");
});

beforeEach(() => {
  hadOwnUserAgent = Object.prototype.hasOwnProperty.call(
    window.navigator,
    "userAgent",
  );
  fixture.destroys.length = 0;
  fixture.pages.length = 0;
  fixture.getDocument.mockReset();
  fixture.loadPdfjs.mockReset();
  fixture.loadPdfjs.mockImplementation(async () => ({
    getDocument: fixture.getDocument,
  }));
  // jsdom reports no Mac, so the default is the native branch; the canvas rows
  // opt in to a host without a PDF plugin.
  actAsHost(WEBVIEW2_WINDOWS);
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(
    function (this: HTMLElement) {
      return this.classList.contains("dt-preview-scroll") ? PANE_WIDTH : 0;
    },
  );
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
    function (this: HTMLElement) {
      if (this.hasAttribute("data-pdf-preview-scroll")) {
        return new DOMRect(0, 0, PANE_WIDTH, VIEWPORT_HEIGHT);
      }
      if (this.dataset.pdfPage) {
        const root = this.closest<HTMLElement>("[data-pdf-preview-scroll]");
        const top = PADDING + offsetInColumn(this) - (root?.scrollTop ?? 0);
        return new DOMRect(0, top, PAGE_CSS_WIDTH, heightOf(this));
      }
      return new DOMRect();
    },
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  if (!hadOwnUserAgent) {
    delete (window.navigator as { userAgent?: string }).userAgent;
  }
});

const PDF_URL = "/api/knowledge-bases/physics/files/textbook.pdf";

function renderPreview(url = PDF_URL, filename = "textbook.pdf") {
  return render(<PdfPreview url={url} filename={filename} />);
}

/** Render on a host without a PDF plugin, with the document already loaded. */
async function openBundled(
  url = PDF_URL,
  pageCount = 3,
  ratios: Record<number, number> = {},
) {
  actAsHost(WKWEBVIEW_MAC);
  fixture.getDocument.mockReturnValue(
    loadingTask(Promise.resolve(buildDocument(pageCount, ratios))),
  );
  const view = renderPreview(url);
  const root = view.container.querySelector<HTMLElement>(
    "[data-pdf-preview-scroll]",
  );
  if (!root) throw new Error("The bundled renderer did not mount");
  installScroller(root);
  await waitFor(() =>
    expect(root.querySelectorAll("[data-pdf-page]").length).toBe(pageCount),
  );
  return { ...view, root };
}

/** Scroll so that `page`'s top edge sits flush with the top of the pane. */
async function scrollToPage(root: HTMLElement, page: number): Promise<void> {
  const element = root.querySelector<HTMLElement>(`[data-pdf-page="${page}"]`);
  if (!element) throw new Error(`Page ${page} is not laid out`);
  root.scrollTop +=
    element.getBoundingClientRect().top - root.getBoundingClientRect().top;
  await act(async () => {
    fireEvent.scroll(root);
    // The component throttles measurement to one frame; wait for that frame.
    await new Promise<void>((resolve) =>
      window.requestAnimationFrame(() => resolve()),
    );
  });
}

function pagesHoldingCanvas(root: HTMLElement): number[] {
  return [...root.querySelectorAll<HTMLElement>("[data-pdf-page]")]
    .filter((page) => page.querySelector("canvas") !== null)
    .map((page) => Number(page.dataset.pdfPage));
}

// -- 1. the dispatch matrix, through the component --------------------------

describe("which renderer a host gets", () => {
  it.each(HOSTS)("$name", async ({ name, userAgent, bundled }) => {
    actAsHost(userAgent);
    // Both branches could decode; only the bundled one may.
    fixture.getDocument.mockReturnValue(
      loadingTask(Promise.resolve(buildDocument(3))),
    );
    const view = renderPreview();

    // The component and the pure function must agree for this exact UA.
    expect(prefersBundledPdfRenderer(userAgent), `${name} / pure function`).toBe(
      bundled,
    );

    if (bundled) {
      await waitFor(() =>
        expect(view.container.querySelectorAll("[data-pdf-page]").length).toBe(3),
      );
      expect(view.container.querySelector("iframe")).toBeNull();
      expect(view.container.querySelector("embed")).toBeNull();
      expect(view.container.querySelector("object")).toBeNull();
      expect(fixture.loadPdfjs).toHaveBeenCalledTimes(1);
      expect(fixture.getDocument).toHaveBeenCalledTimes(1);
      return;
    }

    await waitFor(() =>
      expect(view.container.querySelector("iframe")).toBeTruthy(),
    );
    const frame = view.container.querySelector("iframe")!;
    expect(frame).toHaveAttribute("src", PDF_URL);
    expect(frame).toHaveAttribute("title", "textbook.pdf");
    expect(view.container.querySelector("[data-pdf-page]")).toBeNull();
    expect(view.container.querySelector("canvas")).toBeNull();
    // Let any stray dynamic import settle before claiming it never happened.
    await act(async () => undefined);
    // The whole point of keeping Windows on the iframe: pdf.js is not fetched
    // and no document is decoded, so paging/printing/search stay the host's.
    expect(fixture.loadPdfjs).not.toHaveBeenCalled();
    expect(fixture.getDocument).not.toHaveBeenCalled();
  });

  it("reads the live user agent when it is not given one", () => {
    // The component calls it with no argument (`useState`'s lazy initialiser),
    // so this is the path that actually runs in the app.
    actAsHost(WKWEBVIEW_MAC);
    expect(prefersBundledPdfRenderer()).toBe(true);
    actAsHost(WEBVIEW2_WINDOWS);
    expect(prefersBundledPdfRenderer()).toBe(false);
  });

  it("keeps the canvas renderer behind a dynamic import, so Windows never downloads it", async () => {
    // The branch assertion above cannot see bundling: the harness imports the
    // renderer eagerly. `next/dynamic` is the mechanism that keeps its ~650
    // lines out of the Windows/browser chunk, so a static import creeping in
    // would be an invisible regression that only shows up as bundle weight.
    // Vitest runs with the web root as cwd (see vitest.config.mts).
    const source = await readFile(
      join(process.cwd(), "components/chat/preview/previewers/PdfPreview.tsx"),
      "utf8",
    );
    expect(source).toMatch(
      /dynamic\(\s*\(\)\s*=>\s*import\("\.\/BundledPdfPreview"\)/,
    );
    expect(source).not.toMatch(/^import[\s\S]*?from\s+"\.\/BundledPdfPreview"/m);
  });
});

// -- 2. the native branch must be the pre-change component, verbatim --------

/**
 * `web/components/chat/preview/previewers/PdfPreview.tsx` as of `HEAD` (the
 * commit before the fix), copied verbatim — `git show
 * HEAD:web/components/chat/preview/previewers/PdfPreview.tsx`. It is the
 * reference this suite compares today's `NativePdfPreview` against, because
 * "Windows must not change" means the rendered DOM, not merely "still an
 * iframe".
 */
function PreChangePdfPreview({
  url,
  filename,
}: {
  url: string;
  filename: string;
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

describe("the native branch Windows keeps", () => {
  it("renders exactly the DOM the pre-change implementation rendered", async () => {
    actAsHost(WEBVIEW2_WINDOWS);
    const current = renderPreview();
    const reference = render(
      <PreChangePdfPreview url={PDF_URL} filename="textbook.pdf" />,
    );

    // Before the iframe has loaded — i.e. with the loading overlay up.
    expect(current.container.innerHTML).toBe(reference.container.innerHTML);

    // And after `load`, when the overlay is gone.
    await act(async () => {
      fireEvent.load(current.container.querySelector("iframe")!);
      fireEvent.load(reference.container.querySelector("iframe")!);
    });
    expect(current.container.innerHTML).toBe(reference.container.innerHTML);
  });

  it("keeps the three details that make it look native, named explicitly", async () => {
    actAsHost(WEBVIEW2_WINDOWS);
    const view = renderPreview();
    const shell = view.container.firstElementChild as HTMLElement;

    // The desk colour is `bg-[var(--muted)]/30`, which the canvas branch had to
    // drop (a var() colour has no channels for the `/30` modifier).
    expect(shell).toHaveClass("relative");
    expect(shell).toHaveClass("h-full");
    expect(shell).toHaveClass("w-full");
    expect(shell).toHaveClass("bg-[var(--muted)]/30");

    const overlay = shell.querySelector("div");
    expect(overlay).toHaveClass("pointer-events-none");
    expect(overlay).toHaveClass("absolute");
    expect(overlay).toHaveClass("inset-0");
    expect(overlay).toHaveClass("z-10");
    expect(view.getByText("Loading preview…")).toBeTruthy();

    const frame = shell.querySelector("iframe")!;
    expect(frame.className).toBe("h-full w-full border-0 bg-[var(--background)]");

    await act(async () => {
      fireEvent.load(frame);
    });
    // Only the first `load` flips the flag, and the overlay goes away.
    expect(view.queryByText("Loading preview…")).toBeNull();
    expect(view.container.querySelector("iframe")).toBeTruthy();
  });
});

// -- 3. the canvas branch ---------------------------------------------------

describe("the bundled renderer", () => {
  it("asks pdf.js for the CJK maps, the wasm decoders, the cookie and 1 MB ranges", async () => {
    await openBundled();
    expect(fixture.getDocument).toHaveBeenCalledTimes(1);
    const options = fixture.getDocument.mock.calls[0][0] as Record<
      string,
      unknown
    >;
    expect(options.url).toBe(PDF_URL);
    expect(options.cMapUrl).toBe("http://localhost/pdfjs/cmaps/");
    expect(options.cMapPacked).toBe(true);
    expect(options.wasmUrl).toBe("http://localhost/pdfjs/wasm/");
    expect(options.withCredentials).toBe(true);
    // 64 KB chunks queued dozens of proxied requests before the first page
    // could draw; this is the number that replaced it.
    expect(options.rangeChunkSize).toBe(1048576);
    // Those four are the whole contract; anything else pdf.js defaults.
    expect(Object.keys(options).sort()).toEqual(
      [
        "cMapPacked",
        "cMapUrl",
        "rangeChunkSize",
        "url",
        "wasmUrl",
        "withCredentials",
      ].sort(),
    );
  });

  it("keeps the session cookie off a third-party pdf url", async () => {
    // pdf.js maps `withCredentials` straight to `credentials: "include"`, which
    // makes a cross-origin fetch demand `Access-Control-Allow-Credentials` from
    // the other side; a presigned CDN link answers without it, so attaching the
    // cookie turns "loads on Windows" into "blank on macOS".
    const cases = [
      { url: PDF_URL, credentials: true },
      // Protocol-relative, but still this origin (jsdom serves http://localhost/).
      { url: `//localhost${PDF_URL}`, credentials: true },
      { url: "https://cdn.example.com/textbook.pdf?sig=abc123", credentials: false },
      { url: "//cdn.example.com/textbook.pdf", credentials: false },
    ];

    for (const { url, credentials } of cases) {
      actAsHost(WKWEBVIEW_MAC);
      fixture.getDocument.mockReturnValue(
        loadingTask(Promise.resolve(buildDocument(1))),
      );
      const view = renderPreview(url);
      await waitFor(() => expect(fixture.getDocument).toHaveBeenCalledTimes(1));
      const options = fixture.getDocument.mock.calls[0][0] as {
        withCredentials: boolean;
      };
      expect(options.withCredentials, url).toBe(credentials);
      view.unmount();
      fixture.getDocument.mockReset();
      fixture.getDocument.mockReturnValue(
        loadingTask(Promise.resolve(buildDocument(1))),
      );
    }
  });

  it("hands a page back when it leaves the window, and not on a zoom", async () => {
    const view = await openBundled(PDF_URL, 40);
    await waitFor(() => expect(view.root.querySelectorAll("canvas").length).toBe(2));
    // The draw lands a microtask after the canvas mounts.
    await act(async () => undefined);
    expect(cleanupCallsFor(1)).toBe(0);

    // A zoom redraws at a new width; releasing here would throw away the
    // operator list it just parsed and decode the page a second time.
    await act(async () =>
      (view.getByLabelText("Zoom in") as HTMLButtonElement).click(),
    );
    await act(async () => undefined);
    expect(cleanupCallsFor(1)).toBe(0);

    // Scrolling past is what hands the page back — exactly once, even though
    // that page's render effect ran again for the zoom.
    await scrollToPage(view.root, 20);
    await waitFor(() => expect(cleanupCallsFor(1)).toBe(1));
    expect(cleanupCallsFor(2)).toBe(1);
    // Pages still on screen keep theirs.
    expect(cleanupCallsFor(20)).toBe(0);
  });

  it("lays out 98 pages but mounts canvases only around the viewport", async () => {
    const view = await openBundled(PDF_URL, 98);
    const { root } = view;
    await waitFor(() => expect(root.querySelectorAll("canvas").length).toBeGreaterThan(0));
    expect(root.querySelectorAll("[data-pdf-page]").length).toBe(98);
    expect(pagesHoldingCanvas(root)).toEqual([1, 2]);
    expect(root.querySelectorAll("canvas").length).toBeLessThanOrEqual(3);
    // The toolbar's counter, which lives outside the scroll box.
    expect(view.getByText("Page 1 of 98")).toBeTruthy();
  });

  it("moves the render window with the scroll instead of freezing on page one", async () => {
    const view = await openBundled(PDF_URL, 98);
    await waitFor(() => expect(view.root.querySelectorAll("canvas").length).toBeGreaterThan(0));

    await scrollToPage(view.root, 40);

    expect(view.getByText("Page 40 of 98")).toBeTruthy();
    const drawn = pagesHoldingCanvas(view.root);
    expect(drawn.length).toBeGreaterThan(0);
    expect(drawn.length).toBeLessThanOrEqual(3);
    for (const page of drawn) expect(Math.abs(page - 40)).toBeLessThanOrEqual(1);
    // The frozen-at-page-one bug this replaces: page 1 keeps no canvas.
    expect(view.root.querySelector('[data-pdf-page="1"] canvas')).toBeNull();
  });

  it("disables the paging buttons at the first and last page", async () => {
    const view = await openBundled(PDF_URL, 5);
    const previous = view.getByLabelText("Previous page") as HTMLButtonElement;
    const next = view.getByLabelText("Next page") as HTMLButtonElement;

    expect(previous.disabled).toBe(true);
    expect(next.disabled).toBe(false);
    expect(view.getByText("Page 1 of 5")).toBeTruthy();

    await scrollToPage(view.root, 5);
    await waitFor(() => expect(view.getByText("Page 5 of 5")).toBeTruthy());
    expect(next.disabled).toBe(true);
    expect(previous.disabled).toBe(false);

    // One press of "previous" walks back a page rather than off the document.
    await act(async () => previous.click());
    await waitFor(() => expect(view.getByText("Page 4 of 5")).toBeTruthy());
  });

  it("keeps the zoomed page under the pointer instead of snapping to page one", async () => {
    const view = await openBundled(PDF_URL, 40);
    await scrollToPage(view.root, 20);
    await waitFor(() => expect(view.getByText("Page 20 of 40")).toBeTruthy());
    const before = view.root.scrollTop;

    await act(async () => (view.getByLabelText("Zoom in") as HTMLButtonElement).click());

    const page = view.root.querySelector<HTMLElement>('[data-pdf-page="20"]')!;
    const offset =
      page.getBoundingClientRect().top - view.root.getBoundingClientRect().top;
    // The zoom anchor was page 20's top edge, flush with the pane's top. The
    // page is 1.25x wider (and taller) now, so the scroll position must have
    // moved down by that page's growth — and page 20 must still start exactly
    // where it did. Without the anchor the top would sit ~4 viewports low and
    // the reader would be looking at page 15.
    expect(view.root.scrollTop).toBeGreaterThan(before + 3000);
    expect(Math.abs(offset)).toBeLessThanOrEqual(1);
    expect(view.getByText("Page 20 of 40")).toBeTruthy();
  });

  it("destroys the loading task exactly once when the pane closes", async () => {
    const view = await openBundled();
    await waitFor(() => expect(view.root.querySelectorAll("canvas").length).toBeGreaterThan(0));
    expect(fixture.destroys).toHaveLength(1);
    expect(fixture.destroys[0]).not.toHaveBeenCalled();

    view.unmount();
    expect(fixture.destroys[0]).toHaveBeenCalledTimes(1);
  });

  it("releases the old task and opens the new file at its first page", async () => {
    const view = await openBundled("/api/knowledge-bases/physics/files/first.pdf");
    await waitFor(() => expect(view.root.querySelectorAll("canvas").length).toBeGreaterThan(0));
    await scrollToPage(view.root, 3);

    fixture.getDocument.mockReturnValue(
      loadingTask(Promise.resolve(buildDocument(2))),
    );
    await act(async () => {
      view.rerender(
        <PdfPreview
          url="/api/knowledge-bases/physics/files/second.pdf"
          filename="second.pdf"
        />,
      );
    });

    await waitFor(() =>
      expect(view.root.querySelectorAll("[data-pdf-page]").length).toBe(2),
    );
    expect(fixture.destroys[0]).toHaveBeenCalledTimes(1);
    expect(fixture.destroys[1]).not.toHaveBeenCalled();
    // A new file starts at its own first page.
    expect(view.root.scrollTop).toBe(0);
    expect(view.getByText("Page 1 of 2")).toBeTruthy();
  });

  it("offers a retry and a download when the document cannot be opened", async () => {
    actAsHost(WKWEBVIEW_MAC);
    fixture.getDocument.mockReturnValue(
      loadingTask(Promise.reject(new Error("Invalid PDF structure"))),
    );
    const view = renderPreview("/api/knowledge-bases/physics/files/broken.pdf", "broken.pdf");

    expect(
      await view.findByText("This document could not be opened."),
    ).toBeTruthy();
    expect(view.getByText("Invalid PDF structure")).toBeTruthy();
    const download = view.getByText("Download").closest("a")!;
    expect(download).toHaveAttribute(
      "href",
      "/api/knowledge-bases/physics/files/broken.pdf",
    );
    expect(download).toHaveAttribute("download", "broken.pdf");

    // A failed attempt is not cached: retry runs the loader again.
    fixture.getDocument.mockReturnValue(
      loadingTask(Promise.resolve(buildDocument(2))),
    );
    await act(async () => (view.getByText("Retry") as HTMLElement).click());

    await waitFor(() =>
      expect(view.container.querySelectorAll("[data-pdf-page]").length).toBe(2),
    );
    expect(fixture.getDocument).toHaveBeenCalledTimes(2);
    // The stale error must not survive the retry.
    expect(view.queryByText("This document could not be opened.")).toBeNull();
    expect(view.queryByText("Invalid PDF structure")).toBeNull();
    // The failed attempt's task is released when the retry replaces it.
    expect(fixture.destroys[0]).toHaveBeenCalledTimes(1);
  });

  it("does not report a page as broken when the draw was merely cancelled", async () => {
    actAsHost(WKWEBVIEW_MAC);
    const cancelled = new Error("Rendering cancelled");
    cancelled.name = "RenderingCancelledException";
    fixture.getDocument.mockReturnValue(
      loadingTask(
        Promise.resolve({
          numPages: 2,
          getPage: vi.fn(async (pageNumber: number) => ({
            getViewport: ({ scale }: { scale: number }) => ({
              width: PDF_PAGE_WIDTH * scale,
              height: PDF_PAGE_WIDTH * PDF_PAGE_RATIO * scale,
            }),
            render: () => ({ promise: Promise.reject(cancelled), cancel: () => undefined }),
            cleanup: vi.fn(),
            pageNumber,
          })),
        }),
      ),
    );
    const view = renderPreview();
    await waitFor(() =>
      expect(view.container.querySelectorAll("[data-pdf-page]").length).toBe(2),
    );
    await act(async () => undefined);

    expect(view.queryByText("This page could not be rendered.")).toBeNull();
    expect(view.queryByText("This document could not be opened.")).toBeNull();
  });

  it("does report a page as broken when the draw really failed", async () => {
    // Positive control for the case above: without it, "no error shown" would
    // also pass on a page whose canvas never rendered at all.
    actAsHost(WKWEBVIEW_MAC);
    fixture.getDocument.mockReturnValue(
      loadingTask(
        Promise.resolve({
          numPages: 2,
          getPage: vi.fn(async (pageNumber: number) => ({
            getViewport: ({ scale }: { scale: number }) => ({
              width: PDF_PAGE_WIDTH * scale,
              height: PDF_PAGE_WIDTH * PDF_PAGE_RATIO * scale,
            }),
            render: () => ({
              promise: Promise.reject(new Error("Invalid page content")),
              cancel: () => undefined,
            }),
            cleanup: vi.fn(),
            pageNumber,
          })),
        }),
      ),
    );
    const view = renderPreview();
    expect(
      await view.findAllByText("This page could not be rendered."),
    ).toHaveLength(2);
  });

  it("measures the pane width even on a host without ResizeObserver", async () => {
    // `BundledPdfPreview` measures before it checks for the observer, so the
    // first paint must still be able to size a page on a webview that lacks it.
    vi.stubGlobal("ResizeObserver", undefined);
    const { root } = await openBundled();
    const page = root.querySelector<HTMLElement>('[data-pdf-page="1"]')!;
    expect(page.style.width).toBe(`${PAGE_CSS_WIDTH}px`);
    await waitFor(() => expect(root.querySelector("canvas")).toBeTruthy());
  });

  it("runs the whole lifecycle without a React act() or unmount warning", async () => {
    const errors: string[] = [];
    const setupError = console.error;
    const observer = vi
      .spyOn(console, "error")
      .mockImplementation((...args: unknown[]) => {
        errors.push(args.map(String).join(" "));
        setupError(...args);
      });

    const view = await openBundled(PDF_URL, 30);
    await waitFor(() => expect(view.root.querySelectorAll("canvas").length).toBeGreaterThan(0));
    await scrollToPage(view.root, 12);
    await act(async () => (view.getByLabelText("Zoom in") as HTMLButtonElement).click());
    await act(async () => (view.getByLabelText("Next page") as HTMLButtonElement).click());
    await act(async () => (view.getByLabelText("Fit width") as HTMLButtonElement).click());
    fixture.getDocument.mockReturnValue(
      loadingTask(Promise.resolve(buildDocument(2))),
    );
    await act(async () => {
      view.rerender(<PdfPreview url={`${PDF_URL}?v=2`} filename="textbook.pdf" />);
    });
    await waitFor(() =>
      expect(view.root.querySelectorAll("[data-pdf-page]").length).toBe(2),
    );
    view.unmount();
    observer.mockRestore();

    expect(errors.filter((line) => /act\(|not wrapped in act|unmounted component/i.test(line))).toEqual([]);
  });
});

// -- 3b. mixed page sizes ---------------------------------------------------

describe("documents whose pages are not all one size", () => {
  it("lays each page out at its own ratio, so a taller page is not clipped", async () => {
    // Page 2 is a foldout: 2.2 tall-to-wide, against page 1's A4 1.414. With a
    // single shared ratio and `overflow-hidden`, the bottom of page 2 is cut off
    // and unreachable — the reader sees the top two thirds of every foldout.
    const ratios = { 1: PDF_PAGE_RATIO, 2: 2.2 };
    const { root } = await openBundled(PDF_URL, 2, ratios);
    await waitFor(() => expect(root.querySelectorAll("canvas").length).toBe(2));

    for (const number of [1, 2]) {
      const page = root.querySelector<HTMLElement>(`[data-pdf-page="${number}"]`)!;
      const canvas = page.querySelector("canvas")!;
      const drawn = Number.parseFloat(canvas.style.height);
      expect(drawn).toBeGreaterThan(0);
      // The placeholder must be at least as tall as what is drawn inside it.
      expect(
        Number.parseFloat(page.style.height),
        `page ${number} placeholder is shorter than its drawn page`,
      ).toBeGreaterThanOrEqual(drawn - 1);
    }
  });
});
