/**
 * Which hosts can draw a PDF themselves, and which ones need the app to.
 *
 * The PDF preview used to be an `<iframe src={pdf}>`, which is the cheapest
 * possible preview *where the host ships a PDF plugin*: Chromium (Edge/WebView2,
 * Chrome, Electron with `plugins: true`) and Gecko do. Two of this app's shells
 * do not:
 *
 * - pywebview's Cocoa backend (the macOS `ThinkBuddy.app`) is a WKWebView, which
 *   has no inline PDF renderer at all — the iframe loads, paints nothing, and the
 *   reader sees an empty pane.
 * - the Electron shell (`apps/desktop`) leaves `plugins` off, so Chromium's
 *   `pdfViewerEnabled` stays false and the same blank pane appears.
 *
 * So the decision is deliberately narrow: keep the native viewer everywhere it
 * works (Windows WebView2, every real browser, where it also brings paging,
 * zoom, printing and text selection for free) and only pay for pdf.js on the two
 * macOS shells. WKWebView *does* render PDFs through `<embed>`/`<object>`, but
 * not reliably inside an iframe in an app shell, and not with the toolbar
 * Windows users see — hence "the app draws it" rather than "another embed".
 */

import { desktopPlatform } from "@/lib/version";

/**
 * Browser engines that bring their own PDF viewer. Presence of any of these
 * means the native path is the better one and the app must not take over.
 */
const BROWSER_MARKERS: readonly RegExp[] = [
  /Chrome\//,
  /Chromium\//,
  /Edg\//,
  /OPR\//,
  /Firefox\//,
  // Safari — a real one always names itself; WKWebView's UA stops at
  // "AppleWebKit/… (KHTML, like Gecko)" with no `Version/… Safari/…` suffix.
  // This also keeps iPadOS's desktop-class UA ("Macintosh; Intel Mac OS X")
  // on the native path, where it belongs.
  /Version\/[\d.]+.*Safari\//,
];

/** A Mac, by either spelling Apple uses in its user agents. */
const MAC_AGENT = /Macintosh|Mac OS X/;

/**
 * Does this host need the app to render PDFs itself?
 *
 * Two independent ways to say yes, and the order between them matters:
 *
 * 1. A desktop shell whose platform token says macOS. This must be read from
 *    the `OpenDeepTutor/… (macOS; …)` marker, *before* looking at browser
 *    markers, because an Electron user agent contains `Chrome/` too — testing
 *    for Chrome first would classify the macOS Electron shell as a browser and
 *    leave it with the blank pane it has today.
 * 2. A Mac with no browser identity at all. The macOS pywebview shell injects
 *    no user-agent marker of its own (nothing in `desktop-shell/desktop/*.py`
 *    touches `user_agent`), so its UA is a bare WKWebView string: Macintosh
 *    platform, AppleWebKit, and no engine that ships a PDF viewer. Chrome,
 *    Edge, Firefox and Safari *on* that same Mac still name themselves and keep
 *    the native viewer.
 *
 * What is deliberately *not* used: `window.pywebview`. The bridge object is
 * injected after the document loads, so a reader who opens a PDF during the
 * first seconds — which is exactly when a preview pane gets used — would be
 * classified as a plain browser and shown the blank iframe this whole function
 * exists to avoid. A user agent is present before any app code runs, so it
 * cannot lose that race.
 *
 * Pure and idempotent: pass a user agent to test it, omit it in the browser.
 * With no `navigator` (server render) it answers `false`; the preview itself is
 * client-only, so that answer is never the one a macOS reader sees.
 *
 * No module-level cache on purpose. It would be read once per process and then
 * outlive the page it described — wrong in a test that renders the same
 * component under two hosts, and no cheaper than the call site, which reads it
 * once per mount (`useState`'s lazy initialiser) instead of once per render.
 */
export function prefersBundledPdfRenderer(userAgent?: string | null): boolean {
  const agent =
    userAgent ?? (typeof navigator === "undefined" ? null : navigator.userAgent);
  if (agent === null) return false;
  if (desktopPlatform(agent)?.startsWith("macOS")) return true;
  if (!MAC_AGENT.test(agent)) return false;
  return !BROWSER_MARKERS.some((marker) => marker.test(agent));
}
