import { describe, expect, it } from "vitest";
import { prefersBundledPdfRenderer } from "@/lib/pdf-host";

/**
 * Which hosts get pdf.js, and which keep the native `<iframe>` viewer.
 *
 * The cost of a wrong answer is asymmetric, which is what these cases pin: a
 * false negative on macOS leaves the blank pane this whole change exists to fix,
 * while a false positive takes paging, printing and text selection away from
 * Windows and browser users who had them working.
 *
 * The Electron user agents are the interesting ones: they carry `Chrome/` *and*
 * the `OpenDeepTutor/… (macOS; …)` marker, so a check that looks for browser
 * engine markers before reading the shell's marker classifies the macOS Electron
 * shell as a browser — the exact regression that is invisible on Windows.
 */

/** Apple's own user agents, copied verbatim from the shells' reports. */
const WKWEBVIEW_MAC =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko)";
const SAFARI_MAC =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15";
const CHROME_MAC =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";
const EDGE_MAC =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0";
const FIREFOX_MAC =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:127.0) Gecko/20100101 Firefox/127.0";
/** iPadOS in desktop-class mode reports a Mac; it has a real PDF viewer. */
const SAFARI_IPAD =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1";
const WEBVIEW2_WINDOWS =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0";

/**
 * How the Electron shell builds its user agent: the stock Chromium one for that
 * platform, with the marker appended — `coreSession.setUserAgent(`${…}
 * ${agentMarker(version)}`)` in `apps/desktop/src/main.mjs`.
 */
function electron(platform: string): string {
  const base = platform.startsWith("Windows") ? WEBVIEW2_WINDOWS : CHROME_MAC;
  return `${base} Electron/45.0.0 OpenDeepTutor/1.9.1 (${platform})`;
}

describe("prefersBundledPdfRenderer", () => {
  it("takes over on the macOS pywebview shell, whose UA names no browser", () => {
    expect(prefersBundledPdfRenderer(WKWEBVIEW_MAC)).toBe(true);
  });

  it("takes over on the macOS Electron shell even though its UA says Chrome/", () => {
    expect(prefersBundledPdfRenderer(electron("macOS; arm64"))).toBe(true);
    expect(prefersBundledPdfRenderer(electron("macOS; x64"))).toBe(true);
    // Shells from 1.7.0 sent no architecture.
    expect(prefersBundledPdfRenderer(electron("macOS"))).toBe(true);
  });

  it("leaves every real browser on the native viewer, macOS included", () => {
    expect(prefersBundledPdfRenderer(SAFARI_MAC)).toBe(false);
    expect(prefersBundledPdfRenderer(SAFARI_IPAD)).toBe(false);
    expect(prefersBundledPdfRenderer(CHROME_MAC)).toBe(false);
    expect(prefersBundledPdfRenderer(EDGE_MAC)).toBe(false);
    expect(prefersBundledPdfRenderer(FIREFOX_MAC)).toBe(false);
  });

  it("leaves Windows alone — WebView2, Electron and browsers all have a viewer", () => {
    expect(prefersBundledPdfRenderer(WEBVIEW2_WINDOWS)).toBe(false);
    expect(prefersBundledPdfRenderer(electron("Windows; x64"))).toBe(false);
    expect(prefersBundledPdfRenderer(electron("Windows"))).toBe(false);
  });

  it("answers false when there is no user agent to read", () => {
    // The server-render case: no `navigator`, so nothing is claimed and the
    // preview's own `ssr: false` keeps that answer off the client.
    expect(prefersBundledPdfRenderer("")).toBe(false);
  });
});
