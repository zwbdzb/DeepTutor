import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, expect, it, vi } from "vitest";
import PdfPreview from "@/components/chat/preview/previewers/PdfPreview";

/**
 * The contract of the PDF preview, which is two renderers behind one component.
 *
 * Windows (WebView2) and every real browser keep the native viewer through an
 * `<iframe>` — paging, printing, rotation and text selection included — and must
 * not even download the app's own renderer. The macOS shells cannot: pywebview's
 * Cocoa backend is a WKWebView with no inline PDF renderer, and the Electron
 * shell leaves `plugins` off, so there the pane came up blank while the same file
 * rendered on Windows. For those hosts `./BundledPdfPreview` draws the pages
 * with pdf.js, which is what most of this file asserts.
 *
 * The branch is driven by the *real* `@/lib/pdf-host`, through `navigator.userAgent`
 * — not by mocking that module — so a regression in the detection itself (an
 * Electron user agent that says `Chrome/`, say) fails here rather than passing
 * against a stub.
 *
 * The harness mimics `pdf-zoom-adjacent-page.spec.tsx`: a mocked pdf.js whose
 * pages are 600pt wide, a mocked ResizeObserver, and a `clientWidth` jsdom does
 * not compute.
 */

const fixture = vi.hoisted(() => ({
  getDocument: vi.fn(),
  /** Counts `loadPdfjs()` calls, to prove the native path never decodes. */
  loadPdfjs: vi.fn(),
  /** Every loading task's `destroy`, in the order the tasks were created. */
  destroys: [] as Array<ReturnType<typeof vi.fn>>,
  /** Every page proxy handed out, to check that drawn pages are released. */
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
  const t = (key: string) => key;
  return { useTranslation: () => ({ t }) };
});

/** The hosts, as they actually report themselves. */
const WKWEBVIEW_MAC =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko)";
const SAFARI_MAC =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15";
const WEBVIEW2_WINDOWS =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0";
const ELECTRON_MAC =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Electron/45.1.0 Safari/537.36 OpenDeepTutor/1.7.3 (macOS; arm64)";

const PAGE_WIDTH = 600;
const PAGE_RATIO = 1.414;
const PANE_WIDTH = 648;
/** `px-4` on each side of the page column: the width a page is fitted to. */
const COLUMN_INSET = 32;
/** `py-4` above the first page, and `gap` between pages. */
const COLUMN_TOP = 16;
const PAGE_GAP = 12;
/** The width the component actually commits to every page box. */
const FITTED_WIDTH = PANE_WIDTH - COLUMN_INSET;
/** A page at page one's ratio, and one at ratio 2 — as laid out, in px. */
const A4_HEIGHT = Math.round(FITTED_WIDTH * PAGE_RATIO);
const TALL_HEIGHT = Math.round(FITTED_WIDTH * 2);

function buildDocument(
  pageCount: number,
  ratios: Readonly<Record<number, number>> = {},
) {
  const getPage = vi.fn(async (pageNumber: number) => {
    const page = {
      getViewport: ({ scale }: { scale: number }) => {
        const ratio = ratios[pageNumber] ?? PAGE_RATIO;
        return {
          width: PAGE_WIDTH * scale,
          height: PAGE_WIDTH * ratio * scale,
        };
      },
      render: () => ({ promise: Promise.resolve(), cancel: () => undefined }),
      // pdf.js holds a page's decoded images and operator list until this is
      // called, so the preview has to call it when a page leaves the window.
      cleanup: vi.fn(),
      pageNumber,
    };
    fixture.pages.push(page);
    return page;
  });
  return { numPages: pageCount, getPage };
}

/**
 * Pretend to be a given host.
 *
 * An own accessor on the instance shadows jsdom's `Navigator.prototype` getter;
 * `delete` in the teardown uncovers the original again.
 */
function actAsHost(userAgent: string) {
  Object.defineProperty(window.navigator, "userAgent", {
    configurable: true,
    get: () => userAgent,
  });
}

/** A page's laid-out box, by number. */
function pageAt(root: HTMLElement, pageNumber: number): HTMLElement {
  const page = root.querySelector<HTMLElement>(`[data-pdf-page="${pageNumber}"]`);
  if (!page) throw new Error(`Page ${pageNumber} is missing`);
  return page;
}

/** A page's offset inside the column, from the heights committed so far. */
function pageTop(page: HTMLElement): number {
  let top = COLUMN_TOP;
  for (
    let sibling = page.previousElementSibling;
    sibling;
    sibling = sibling.previousElementSibling
  ) {
    top += Number.parseFloat((sibling as HTMLElement).style.height) + PAGE_GAP;
  }
  return top;
}

/**
 * Give the pane a viewport and the pages real positions.
 *
 * jsdom lays nothing out — every rect is zero, so no page ever covers the
 * viewport and the render window would stay on page one forever. These rects are
 * derived from the inline heights the component commits, which is what a browser
 * does with the same CSS.
 */
function stubScrollGeometry() {
  const VIEWPORT_HEIGHT = 500;
  const VIEWPORT_TOP = 80;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
    function (this: HTMLElement) {
      if (this.classList.contains("dt-preview-scroll")) {
        return new DOMRect(120, VIEWPORT_TOP, PANE_WIDTH, VIEWPORT_HEIGHT);
      }
      if (this.dataset.pdfPage) {
        const scrollTop = this.closest<HTMLElement>(".dt-preview-scroll")?.scrollTop ?? 0;
        return new DOMRect(
          120 + COLUMN_INSET,
          VIEWPORT_TOP + pageTop(this) - scrollTop,
          Number.parseFloat(this.style.width),
          Number.parseFloat(this.style.height),
        );
      }
      return new DOMRect();
    },
  );
  // The component measures one frame after a scroll; run that frame on the next
  // macrotask so `waitFor` can await it instead of racing a 16 ms animation.
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) =>
    window.setTimeout(() => callback(0), 0),
  );
  vi.stubGlobal("cancelAnimationFrame", (handle: number) =>
    window.clearTimeout(handle),
  );
}

let hadOwnUserAgent = false;

/**
 * Warm the lazily-imported renderer once, outside any `waitFor`.
 *
 * `next/dynamic` transforms and loads that module on its first mount, which in
 * this suite can take longer than a `waitFor` window and would make the first
 * bundled-host test look like a failed branch.
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
  // jsdom reports no Mac at all, so the default here is the Windows/browser
  // branch; the canvas cases opt in to a host without a PDF plugin.
  actAsHost(WEBVIEW2_WINDOWS);
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(
    function (this: HTMLElement) {
      return this.classList.contains("dt-preview-scroll") ? PANE_WIDTH : 0;
    },
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  if (!hadOwnUserAgent) delete (window.navigator as { userAgent?: string }).userAgent;
});

/** Render on a host without a PDF plugin (a macOS shell). */
function openBundled(
  url = "/files/attachments/textbook.pdf",
  pageCount = 3,
  userAgent = WKWEBVIEW_MAC,
) {
  actAsHost(userAgent);
  fixture.getDocument.mockReturnValue(
    loadingTask(Promise.resolve(buildDocument(pageCount))),
  );
  const view = render(<PdfPreview url={url} filename="textbook.pdf" />);
  return { ...view, root: () => view.container.querySelector<HTMLElement>(".dt-preview-scroll") };
}

async function openPreview(
  url = "/files/attachments/textbook.pdf",
  pageCount = 3,
) {
  const view = openBundled(url, pageCount);
  await waitFor(() =>
    expect(view.container.querySelectorAll("[data-pdf-page]").length).toBe(pageCount),
  );
  return { ...view, root: view.root()! };
}

// -- hosts that keep the native viewer -------------------------------------

it("keeps the native viewer on Windows WebView2, and never downloads the bundled renderer", () => {
  actAsHost(WEBVIEW2_WINDOWS);
  const view = render(
    <PdfPreview url="/files/attachments/textbook.pdf" filename="textbook.pdf" />,
  );

  const frame = view.container.querySelector("iframe");
  expect(frame).toHaveAttribute("src", "/files/attachments/textbook.pdf");
  expect(frame).toHaveAttribute("title", "textbook.pdf");
  // The whole point of the branch: the app's renderer is neither mounted nor
  // fetched, so Windows keeps paging, printing, rotation and text selection.
  expect(view.container.querySelector("canvas")).toBeNull();
  expect(view.container.querySelector("[data-pdf-page]")).toBeNull();
  expect(fixture.loadPdfjs).not.toHaveBeenCalled();
  expect(fixture.getDocument).not.toHaveBeenCalled();
});

it("keeps the native viewer in Safari on a Mac, which has one of its own", () => {
  actAsHost(SAFARI_MAC);
  const view = render(
    <PdfPreview url="/files/attachments/textbook.pdf" filename="textbook.pdf" />,
  );

  expect(view.container.querySelector("iframe")).toHaveAttribute(
    "src",
    "/files/attachments/textbook.pdf",
  );
  expect(view.container.querySelector("canvas")).toBeNull();
  expect(fixture.loadPdfjs).not.toHaveBeenCalled();
});

// -- hosts that need the app to draw ---------------------------------------

it("draws the pages itself on the macOS Electron shell, whose UA also says Chrome/", async () => {
  actAsHost(ELECTRON_MAC);
  fixture.getDocument.mockReturnValue(
    loadingTask(Promise.resolve(buildDocument(3))),
  );
  const view = render(
    <PdfPreview url="/files/attachments/textbook.pdf" filename="textbook.pdf" />,
  );

  await waitFor(() =>
    expect(view.container.querySelectorAll("[data-pdf-page]").length).toBe(3),
  );
  expect(view.container.querySelector("iframe")).toBeNull();
  expect(view.container.querySelector("embed")).toBeNull();
  await waitFor(() => expect(view.container.querySelector("canvas")).toBeTruthy());
  expect(fixture.loadPdfjs).toHaveBeenCalledTimes(1);
});

it("draws the pages itself on the macOS WKWebView shell", async () => {
  const view = await openPreview();
  expect(view.container.querySelector("iframe")).toBeNull();
  expect(view.container.querySelector("embed")).toBeNull();
  // One page laid out, waiting for a canvas: the layout is what makes the
  // scrollbar honest before anything has been drawn.
  expect(view.root.querySelectorAll("[data-pdf-page]").length).toBe(3);
  await waitFor(() => expect(view.root.querySelector("canvas")).toBeTruthy());
});

it("asks pdf.js for the CJK character maps, the wasm decoders and the session cookie", async () => {
  await openPreview("/files/attachments/textbook.pdf");
  expect(fixture.getDocument).toHaveBeenCalledTimes(1);
  expect(fixture.getDocument).toHaveBeenCalledWith(
    expect.objectContaining({
      url: "/files/attachments/textbook.pdf",
      cMapUrl: "http://localhost/pdfjs/cmaps/",
      cMapPacked: true,
      wasmUrl: "http://localhost/pdfjs/wasm/",
      withCredentials: true,
    }),
  );
});

it("keeps a 98-page document to a window of canvases rather than 98 of them", async () => {
  const view = await openPreview("/files/attachments/textbook.pdf", 98);
  await waitFor(() =>
    expect(view.root.querySelectorAll("canvas").length).toBeGreaterThan(0),
  );
  // Page 1 plus its one-page render margin; the other 96 stay placeholders.
  expect(view.root.querySelectorAll("canvas").length).toBeLessThanOrEqual(2);
  expect(view.root.querySelectorAll("[data-pdf-page]").length).toBe(98);
});

it("lays a page out at its own ratio instead of clipping it to page one's", async () => {
  stubScrollGeometry();
  actAsHost(WKWEBVIEW_MAC);
  // 600×848 (A4) then two 600×1200 pages: a photographed cover or a fold-out
  // bound in with a text block of another size. Laying every page out at page
  // one's ratio left those pages taller than their box, and `overflow-hidden`
  // then cut the bottom half away — missing content, not a scroll offset.
  fixture.getDocument.mockReturnValue(
    loadingTask(Promise.resolve(buildDocument(3, { 2: 2, 3: 2 }))),
  );
  const view = render(
    <PdfPreview url="/files/attachments/mixed.pdf" filename="mixed.pdf" />,
  );
  // The renderer is dynamically imported, so the pane mounts a tick after the
  // preview does — on the first test of a filtered run that tick is real.
  const root = await waitFor(() => {
    const pane = view.container.querySelector<HTMLElement>(".dt-preview-scroll");
    if (!pane) throw new Error("The page pane has not mounted yet");
    return pane;
  });

  await waitFor(() =>
    expect(pageAt(root, 1).style.height).toBe(`${A4_HEIGHT}px`),
  );
  // Page 2 is one page below the viewport, inside the render margin, so it has
  // already parsed and reported its real ratio.
  await waitFor(() =>
    expect(pageAt(root, 2).style.height).toBe(`${TALL_HEIGHT}px`),
  );
  // Page 3 is outside the window and therefore unmeasured: it still borrows page
  // one's ratio, which is exactly the estimate a page that far out can afford.
  expect(pageAt(root, 3).style.height).toBe(`${A4_HEIGHT}px`);

  // Scroll page 2 to the top of the pane, which is where a reader would see it.
  await act(async () => {
    root.scrollTop = pageTop(pageAt(root, 2));
    fireEvent.scroll(root);
  });

  // Bringing page 3 into the window is what measures it; it corrects itself.
  await waitFor(() =>
    expect(pageAt(root, 3).style.height).toBe(`${TALL_HEIGHT}px`),
  );
  expect(pageAt(root, 2).style.height).toBe(`${TALL_HEIGHT}px`);
  // Page one keeps its own height: the measurement is per page.
  expect(pageAt(root, 1).style.height).toBe(`${A4_HEIGHT}px`);
});

it("sends the session cookie to the app's own files, and not to a third-party URL", async () => {
  await openPreview("/api/files/attachments/textbook.pdf");
  expect(fixture.getDocument).toHaveBeenLastCalledWith(
    expect.objectContaining({ withCredentials: true }),
  );

  // `resolveSourceUrl` passes a presigned third-party link through as it is.
  // `credentials: "include"` would then require `Access-Control-Allow-Credentials`
  // from the other side, which a CDN link does not send, so the fetch would fail
  // on the one host that draws pages itself.
  await openPreview("https://cdn.example.com/textbook.pdf?sig=abc123");
  expect(fixture.getDocument).toHaveBeenLastCalledWith(
    expect.objectContaining({ withCredentials: false }),
  );
});

it("hands a drawn page back to pdf.js once it leaves the render window", async () => {
  stubScrollGeometry();
  actAsHost(WKWEBVIEW_MAC);
  fixture.getDocument.mockReturnValue(
    loadingTask(Promise.resolve(buildDocument(8))),
  );
  const view = render(
    <PdfPreview url="/files/attachments/textbook.pdf" filename="textbook.pdf" />,
  );
  const root = await waitFor(() => {
    const pane = view.container.querySelector<HTMLElement>(".dt-preview-scroll");
    if (!pane) throw new Error("The page pane has not mounted yet");
    return pane;
  });
  await waitFor(() =>
    expect(root.querySelectorAll("canvas").length).toBeGreaterThan(0),
  );

  const released = (pageNumber: number) =>
    fixture.pages.filter(
      (page) => page.pageNumber === pageNumber && page.cleanup.mock.calls.length > 0,
    ).length;

  // Scroll to page 6: page 1 is far outside the ±1 window and unmounts.
  await act(async () => {
    root.scrollTop = pageTop(pageAt(root, 6));
    fireEvent.scroll(root);
  });

  // The bitmap alone would not be enough — pdf.js keeps the page's decoded
  // images and operator list until this call, tens of MB a page for a scan.
  await waitFor(() => expect(released(1)).toBeGreaterThan(0));
  // A page still on screen keeps its parse: releasing it would re-decode on the
  // next scroll for nothing.
  expect(released(6)).toBe(0);
  // Releasing pages must not be done by tearing down the document: that is what
  // a file change does, and it would leave the reader with a dead renderer.
  expect(fixture.destroys[0]).not.toHaveBeenCalled();
});

it("releases the worker and the bitmap when the file changes or the pane closes", async () => {
  const view = await openPreview("/files/attachments/first.pdf");
  await waitFor(() => expect(view.root.querySelector("canvas")).toBeTruthy());

  // A different file must not inherit the previous document's pages.
  fixture.getDocument.mockReturnValue(
    loadingTask(Promise.resolve(buildDocument(2))),
  );
  await act(async () => {
    view.rerender(
      <PdfPreview url="/files/attachments/second.pdf" filename="second.pdf" />,
    );
  });
  await waitFor(() =>
    expect(view.root.querySelectorAll("[data-pdf-page]").length).toBe(2),
  );
  // The first file's worker port is released the moment its file is replaced.
  expect(fixture.destroys[0]).toHaveBeenCalledTimes(1);

  view.unmount();
  expect(fixture.destroys[1]).toHaveBeenCalledTimes(1);
});

it("offers a retry and a download when the document cannot be opened", async () => {
  actAsHost(WKWEBVIEW_MAC);
  fixture.getDocument.mockReturnValue(
    // pdf.js rejects with its own error subclasses, which is what the pane's
    // message has to survive.
    loadingTask(Promise.reject(new Error("Invalid PDF structure"))),
  );
  const view = render(
    <PdfPreview url="/files/attachments/broken.pdf" filename="broken.pdf" />,
  );

  expect(await view.findByText("This document could not be opened.")).toBeTruthy();
  expect(view.getByText("Invalid PDF structure")).toBeTruthy();
  expect(view.getByText("Download").closest("a")).toHaveAttribute(
    "href",
    "/files/attachments/broken.pdf",
  );

  // Retrying must run the loader again — a failed attempt is not cached.
  fixture.getDocument.mockReturnValue(
    loadingTask(Promise.resolve(buildDocument(2))),
  );
  await act(async () => {
    view.getByText("Retry").click();
  });
  await waitFor(() =>
    expect(view.container.querySelectorAll("[data-pdf-page]").length).toBe(2),
  );
  expect(fixture.getDocument).toHaveBeenCalledTimes(2);
});
