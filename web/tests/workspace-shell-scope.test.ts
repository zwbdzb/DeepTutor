import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

const source = (relative: string) =>
  fs.readFileSync(path.resolve(process.cwd(), relative), "utf8");

/**
 * The layouts are largely prose: their docblocks explain the ordering, and a
 * stray `<AppShell` or `</WorkspaceRuntimeBoundary>` written in a comment would
 * be found by `indexOf` before the real one. Strip block comments (`/* *\/`,
 * which also covers the JSX `{/* … *\/}` ones) so every assertion is about code.
 */
const code = (relative: string) =>
  source(relative).replace(/\/\*[\s\S]*?\*\//g, "");

/** Where `needle` first appears, or -1 — the layouts are small and ordered by hand. */
const at = (text: string, needle: string) => text.indexOf(needle);
/** Where `needle` last appears, for the page-level boundary under the runtimes. */
const lastAt = (text: string, needle: string) => text.lastIndexOf(needle);

/**
 * `needle` is present (so a rename cannot make the comparison vacuously true —
 * `indexOf` answers -1) and starts before `later`, an index already found in
 * the same source.
 */
function assertBefore(text: string, needle: string, later: number, message: string) {
  const index = at(text, needle);
  assert.notEqual(index, -1, `${message} (expected to find ${needle})`);
  assert.ok(index < later, message);
}

/**
 * The runtimes each layout scopes by workspace, innermost last.
 *
 * They reset by unmounting: the board store drops its state when its last owner
 * unmounts (`lib/task-board-store`), the reader's open document is plain
 * provider state, and the calendar's sync restarts on mount. That is why a
 * workspace switch still resets them — and why they must stay *inside* the
 * scope boundary. The utility group scopes the calendar only; asserting that
 * here, rather than in the shell test below, is what makes hoisting the
 * calendar out fail this test instead of slipping past every other one.
 */
const SCOPED_RUNTIMES: ReadonlyArray<{
  file: string;
  runtimes: readonly string[];
  innermost: string;
}> = [
  {
    file: "app/(workspace)/layout.tsx",
    runtimes: ["<CalendarRuntime>", "<TaskBoardRuntime>", "<ReadingProvider>"],
    innermost: "<ReadingProvider>",
  },
  {
    file: "app/(utility)/layout.tsx",
    runtimes: ["<CalendarRuntime>"],
    innermost: "<CalendarRuntime>",
  },
];

/**
 * The workspace scope boundary remounts everything below it when the URL names
 * another workspace (see WorkspaceRuntimeBoundary). The shell and its sidebar
 * must therefore sit above it: their state — the conversation list spanning
 * every workspace, the collapsed folders, the scroll position — is identical in
 * every workspace, and remounting it made the whole column flash a skeleton on
 * every cross-workspace click (#sidebar-flash).
 */
test("the shell and its sidebar outlive a workspace switch", () => {
  const layout = code("app/(workspace)/layout.tsx");
  const shell = at(layout, "<AppShell");
  const boundary = at(layout, "<WorkspaceRuntimeBoundary>");
  assert.notEqual(shell, -1, "the workspace layout still renders the AppShell");
  assert.notEqual(boundary, -1, "the workspace layout still scopes by workspace");
  assert.ok(
    shell < boundary,
    "AppShell (and the WorkspaceSidebar it is given) must be outside the scope boundary",
  );
  // `sidebar={…}` is a prop of the AppShell, so hoisting the shell hoists it —
  // but only while that prop is still written with a WorkspaceSidebar in it.
  assertBefore(
    layout,
    "sidebar={<WorkspaceSidebar />}",
    boundary,
    "the sidebar is rendered by the hoisted AppShell, above the scope boundary",
  );
  // The page itself is still inside: a switch must reset content state.
  assert.notEqual(at(layout, "{children}"), -1, "route content is still rendered");
  assert.ok(
    boundary < at(layout, "{children}"),
    "route content must stay inside the scope boundary",
  );
});

test("the chat runtime feeding the sidebar is above the shell", () => {
  const layout = code("app/(workspace)/layout.tsx");
  const shell = at(layout, "<AppShell");
  assert.notEqual(shell, -1, "the workspace layout still renders the AppShell");
  assertBefore(
    layout,
    "<ChatRuntimeProvider>",
    shell,
    "WorkspaceSidebar reads useChatStateAdapter; the runtime must outlive the switch",
  );
});

test("workspace-scoped runtimes stay inside the boundary", () => {
  for (const { file, runtimes, innermost } of SCOPED_RUNTIMES) {
    const layout = code(file);
    const boundary = at(layout, "<WorkspaceRuntimeBoundary>");
    const boundaryEnd = at(layout, "</WorkspaceRuntimeBoundary>");
    assert.notEqual(boundary, -1, `${file} renders the scope boundary`);
    assert.notEqual(boundaryEnd, -1, `${file} closes the scope boundary`);
    assert.ok(boundary < boundaryEnd, `${file}: the boundary opens before it closes`);
    for (const runtime of runtimes) {
      const index = at(layout, runtime);
      assert.notEqual(index, -1, `${file} still renders ${runtime}`);
      assert.ok(
        boundary < index && index < boundaryEnd,
        `${file}: ${runtime} resets with the scope, so it belongs inside WorkspaceRuntimeBoundary`,
      );
    }
    assertBefore(
      layout,
      innermost,
      lastAt(layout, "<Suspense"),
      `${file}: the scoped runtimes must sit above the page-level Suspense`,
    );
  }
});

/**
 * A picker rendered from a `next/dynamic({ssr:false})` chunk suspends on its
 * first open, and a click-driven setState is not a transition — React answers
 * with the nearest fallback and unmounts what is under it. AppShell's boundary
 * is above the runtimes now, so the page needs one below them.
 */
test("the page-level Suspense sits below the scoped runtimes", () => {
  for (const { file, innermost } of SCOPED_RUNTIMES) {
    const layout = code(file);
    const pageSuspense = lastAt(layout, "<Suspense");
    assert.notEqual(pageSuspense, -1, `${file} renders a Suspense boundary`);
    assertBefore(
      layout,
      innermost,
      pageSuspense,
      `${file}: the runtimes must sit above the page-level Suspense`,
    );
    assert.ok(
      pageSuspense < at(layout, "{children}"),
      `${file}: the page-level Suspense must wrap {children}`,
    );
  }
});

test("useSearchParams() in the shell stays under a Suspense boundary", () => {
  for (const { file } of SCOPED_RUNTIMES) {
    const layout = code(file);
    const shell = at(layout, "<AppShell");
    assert.notEqual(shell, -1, `${file} renders the AppShell`);
    assertBefore(
      layout,
      "<Suspense",
      shell,
      `${file}: the shell renders below a Suspense boundary (CSR bailout otherwise)`,
    );
  }
});

test("the AppShell keeps its own boundary around the page", () => {
  // The layouts' outer <Suspense> renders nothing while it waits, so it
  // cannot stand in for this one: on the first mount of /chat, the page
  // suspends on its `next/dynamic` chunks at once. Without AppShell's own
  // boundary React answers with the outer one and the whole shell — sidebar
  // included — renders as null for a frame.
  const appShell = code("components/layout/AppShell.tsx");
  const page = at(appShell, "{children}");
  const boundaryEnd = at(appShell, "</Suspense>");
  assert.notEqual(page, -1, "AppShell still renders the page");
  assert.notEqual(boundaryEnd, -1, "AppShell still closes its boundary");
  assertBefore(
    appShell,
    "<Suspense",
    page,
    "AppShell's own boundary must keep wrapping the page",
  );
  // Wrapping, not merely preceding: a boundary that closed before the page
  // would leave the page outside it and read as a pass on the check above.
  assert.ok(
    page < boundaryEnd,
    "the page is inside AppShell's boundary, not merely after it",
  );
});

test("the utility shell is hoisted the same way", () => {
  const layout = code("app/(utility)/layout.tsx");
  const shell = at(layout, "<AppShell");
  const boundary = at(layout, "<WorkspaceRuntimeBoundary>");
  assert.notEqual(shell, -1, "the utility layout renders the AppShell");
  assert.notEqual(boundary, -1, "the utility layout still scopes by workspace");
  assert.ok(
    shell < boundary,
    "the utility sidebar must not remount on a workspace switch either",
  );
  assertBefore(
    layout,
    "sidebar={<UtilitySidebar />}",
    boundary,
    "the utility sidebar is passed to the hoisted AppShell",
  );
});
