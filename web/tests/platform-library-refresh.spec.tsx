import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useKnowledgeBases } from "@/hooks/useKnowledgeBases";

const fixture = vi.hoisted(() => ({ list: vi.fn() }));
vi.mock("@/features/knowledge/api/catalog", async (original) => ({
  ...await original<typeof import("@/features/knowledge/api/catalog")>(),
  listKnowledgeBases: fixture.list,
  listRagProviders: async () => [],
  getKnowledgeUploadPolicy: async () => { throw new Error("default"); },
}));

const local = { id: "account:kb:private", name: "private", status: "ready" };
const gift = { id: "account:kb:Gift", name: "Gift", source: "user", read_only: false, status: "ready", metadata: { type: "weknora" } };

beforeEach(() => {
  vi.useFakeTimers();
  fixture.list.mockReset().mockResolvedValue([local, gift]);
});
afterEach(() => vi.useRealTimers());

it("loads ordinary gifts, refreshes on focus and interval, and reflects deleted connections", async () => {
  const { result, unmount } = renderHook(() => useKnowledgeBases());
  await act(async () => {});
  expect(result.current.kbs.map(kb => kb.id)).toEqual([local.id, gift.id]);
  fixture.list.mockResolvedValue([local, { ...gift, name: "Renamed gift" }]);
  await act(async () => { window.dispatchEvent(new Event("focus")); });
  expect(result.current.kbs[1].name).toBe("Renamed gift");
  fixture.list.mockResolvedValue([local]);
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
  expect(result.current.kbs).toEqual(expect.arrayContaining([expect.objectContaining(local)]));
  expect(result.current.kbs.some(kb => kb.id === gift.id)).toBe(false);
  expect(fixture.list).toHaveBeenLastCalledWith({ force: true });
  const count = fixture.list.mock.calls.length;
  unmount();
  await act(async () => {
    window.dispatchEvent(new Event("focus"));
    await vi.advanceTimersByTimeAsync(60_000);
  });
  expect(fixture.list).toHaveBeenCalledTimes(count);
});
