import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { expect, it } from "vitest";
import {
  NAV_BY_HREF,
  PRIMARY_NAV_HREFS,
  DEFAULT_COLLAPSED_NAV,
  isNavActive,
  navVisibleUnderPolicy,
} from "@/components/sidebar/nav-entries";
import { resolveNavLayout } from "@/lib/sidebar-layout";

/**
 * `/personalization` is the experts-skills-connectors hub. It ships in the
 * classic sidebar (``components/sidebar/nav-entries.ts``) as an account-level
 * entry: visible by default, sitting directly under personalized learning, and
 * hidden whenever a learning policy is in effect.
 */

const HREF = "/personalization";
const LABEL = "Experts · Skills · Connectors";
const LOCALES_DIR = path.join(process.cwd(), "locales");

function shippedLocales() {
  return readdirSync(LOCALES_DIR, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .sort();
}

function readCatalog(locale: string) {
  return JSON.parse(
    readFileSync(path.join(LOCALES_DIR, locale, "app.json"), "utf8"),
  ) as Record<string, string>;
}

it("ships an entry that points at the personalization hub", () => {
  const entry = NAV_BY_HREF.get(HREF);
  expect(entry).toBeDefined();
  expect(entry?.href).toBe(HREF);
  expect(entry?.icon).toBeTruthy();
  expect(entry?.label).toBe(LABEL);
  // A duplicated href would silently collapse inside NAV_BY_HREF.
  expect(new Set(PRIMARY_NAV_HREFS).size).toBe(PRIMARY_NAV_HREFS.length);
});

it("sits directly under personalized learning", () => {
  const learningIndex = PRIMARY_NAV_HREFS.indexOf("/learning");
  expect(learningIndex).toBeGreaterThanOrEqual(0);
  expect(PRIMARY_NAV_HREFS.indexOf(HREF)).toBe(learningIndex + 1);
});

it("is visible by default rather than folded into More", () => {
  expect(PRIMARY_NAV_HREFS).toContain(HREF);
  expect(DEFAULT_COLLAPSED_NAV).not.toContain(HREF);
});

it("stays visible for accounts without a learning policy", () => {
  expect(navVisibleUnderPolicy(HREF, null)).toBe(true);
});

it("ships a translated label in every locale the build loads", () => {
  const locales = shippedLocales();
  expect(locales).toContain("en");
  expect(locales).toContain("zh");

  const untranslated = locales.filter((locale) => {
    const catalog = readCatalog(locale);
    const value = catalog[LABEL];
    return typeof value !== "string" || value.trim().length === 0;
  });
  expect(untranslated).toEqual([]);

  // The Chinese label is a product requirement, not just any translation.
  expect(readCatalog("zh")[LABEL]).toBe("专家·技能·连接器");
});

it("hides under a learning policy exactly like the other account entries", () => {
  const policy = ["chat"];
  expect(navVisibleUnderPolicy(HREF, policy)).toBe(
    navVisibleUnderPolicy("/agents", policy),
  );
  expect(navVisibleUnderPolicy(HREF, policy)).toBe(
    navVisibleUnderPolicy("/space", policy),
  );
  expect(navVisibleUnderPolicy(HREF, [])).toBe(false);
  // Declaring `surfaces` would make the entry policy-dependent; it must not.
  expect(NAV_BY_HREF.get(HREF)?.surfaces).toBeUndefined();
});

it("marks itself active on every personalization page", () => {
  for (const pathname of [
    "/space/personas",
    "/space/personas/abc",
    "/space/skills",
    "/space/mcp",
    "/space/cli-apps",
    "/memory",
    "/memory/l1",
    "/agents",
  ]) {
    expect(isNavActive(pathname, HREF)).toBe(true);
    // "Learning Space" must not also light up for the same path.
    expect(isNavActive(pathname, "/space")).toBe(false);
  }
  expect(isNavActive("/space", "/space")).toBe(true);
  expect(isNavActive("/space", HREF)).toBe(false);
  // `/agents` keeps its own row lit alongside the shelf entry.
  expect(isNavActive("/agents", "/agents")).toBe(true);
});

it("keeps Learning Space lit on its top-level resource surfaces", () => {
  for (const pathname of [
    "/notebooks",
    "/notebooks/abc",
    "/knowledge-bases",
    "/knowledge-bases/kb-1",
    "/co-writer",
    "/co-writer/doc-1",
  ]) {
    expect(isNavActive(pathname, "/space")).toBe(true);
    expect(isNavActive(pathname, HREF)).toBe(false);
  }
});

it("reaches profiles whose saved sidebar order predates the entry", () => {
  const legacyOrder = [
    "/chat",
    "/partners",
    "/learning",
    "/space",
    "/calendar",
    "/kanban",
    "/co-writer",
    "/agents",
  ];
  const resolved = resolveNavLayout(
    PRIMARY_NAV_HREFS,
    { order: legacyOrder, collapsed: DEFAULT_COLLAPSED_NAV },
    DEFAULT_COLLAPSED_NAV,
  );
  // Woven in beside the neighbour it follows, never appended or dropped.
  expect(resolved.order.indexOf(HREF)).toBe(
    resolved.order.indexOf("/learning") + 1,
  );
  expect(resolved.visible).toEqual([
    "/chat",
    "/partners",
    "/learning",
    "/personalization",
    "/space",
    "/calendar",
    "/kanban",
  ]);
});
