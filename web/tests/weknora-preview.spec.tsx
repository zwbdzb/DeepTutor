import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import KbDocumentList from "@/components/knowledge/KbDocumentList";
import KbFilesTab from "@/components/knowledge/KbFilesTab";
import { kbDetailSections, type KnowledgeBase } from "@/lib/knowledge-helpers";
import { previewKindFor, type FilePreviewSource } from "@/components/chat/preview/previewerFor";

const fixture = vi.hoisted(() => ({ list: vi.fn(), t: (key: string) => key }));
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: fixture.t }) }));
vi.mock("@/features/knowledge/api/files", async (original) => ({
  ...await original<typeof import("@/features/knowledge/api/files")>(),
  listKnowledgeBaseFiles: fixture.list,
}));
vi.mock("@/components/knowledge/KbFilePreview", () => ({
  default: ({ source, metaSuffix }: { source: FilePreviewSource | null; metaSuffix?: React.ReactNode }) => (
    <div>
      {source && <div data-testid="preview" data-url={source.url} data-conversion={source.officePdfUrl} data-kind={previewKindFor(source)}>{source.filename}</div>}
      {metaSuffix}
    </div>
  ),
}));

const kb: KnowledgeBase = { id: "workspace:team:kb:remote", name: "remote", status: "ready", metadata: { type: "weknora" } };

beforeEach(() => fixture.list.mockReset().mockResolvedValue([]));

it.each(["resolve", "reject"])("keeps the new knowledge base when the previous request later %s", async (outcome) => {
  let resolveOld!: (files: { id: string; name: string }[]) => void;
  let rejectOld!: (error: Error) => void;
  fixture.list.mockImplementationOnce(() => new Promise((resolve, reject) => {
    resolveOld = resolve;
    rejectOld = reject;
  }));
  const props = { kbName: "old-kb", readOnly: true, selectedFile: null, onSelect: vi.fn(), collapsed: false, onToggleCollapsed: vi.fn() };
  const view = render(<KbDocumentList {...props} refreshKey={0} />);
  fixture.list.mockResolvedValue([{ id: "fresh", name: "fresh.pdf" }]);
  view.rerender(<KbDocumentList {...props} kbName="new-kb" />);
  expect(await screen.findByRole("button", { name: "fresh.pdf" })).toBeInTheDocument();
  await act(async () => {
    if (outcome === "resolve") resolveOld([{ id: "stale", name: "stale.pdf" }]);
    else rejectOld(new Error("old request failed"));
  });
  expect(screen.getByRole("button", { name: "fresh.pdf" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "stale.pdf" })).not.toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it.each([
  ["guide.pdf", "pdf"], ["guide.docx", "docx"], ["guide.xlsx", "xlsx"],
  ["guide.pptx", "pptx"], ["guide.md", "markdown"], ["guide.txt", "text"], ["photo.png", "image"],
])("passes %s to the existing %s renderer through the authenticated proxy", async (name, kind) => {
  fixture.list.mockResolvedValue([{ id: "doc-1", name, type: "file" }]);
  render(<KbFilesTab kb={kb} />);
  fireEvent.click(await screen.findByRole("button", { name }));
  const preview = screen.getByTestId("preview");
  expect(preview).toHaveAttribute("data-kind", kind);
  expect(preview).toHaveAttribute("data-url", "/api/knowledge-bases/workspace%3Ateam%3Akb%3Aremote/remote-file-preview/doc-1");
  expect(preview).toHaveAttribute("data-conversion", "/api/knowledge-bases/workspace%3Ateam%3Akb%3Aremote/remote-file-preview/doc-1?format=pdf");
  expect(kbDetailSections(kb)).toEqual(["files", "settings"]);
});

it("keeps same-name files distinct and switches PDF to the parsed text fallback", async () => {
  fixture.list.mockResolvedValue([
    { id: "doc-1", name: "guide.pdf", mime_type: "application/pdf" },
    { id: "doc-2", name: "guide.pdf", mime_type: "application/pdf" },
  ]);
  render(<KbFilesTab kb={kb} />);
  const documents = await screen.findAllByRole("button", { name: "guide.pdf" });
  fireEvent.click(documents[1]);
  expect(screen.getByTestId("preview").getAttribute("data-url")).toMatch(/doc-2$/);
  fireEvent.click(screen.getByRole("button", { name: "Parsed text" }));
  expect(screen.getByTestId("preview")).toHaveAttribute("data-kind", "text");
  expect(screen.getByTestId("preview").getAttribute("data-url")).toMatch(/remote-file-preview-text\/doc-2$/);
  fireEvent.click(documents[0]);
  expect(screen.getByTestId("preview")).toHaveAttribute("data-kind", "pdf");
  expect(screen.getByTestId("preview").getAttribute("data-url")).toMatch(/doc-1$/);
});

it("shows connection failures and refreshes remote files without exposing write controls", async () => {
  fixture.list.mockRejectedValueOnce(new Error("connection failed"));
  render(<KbFilesTab kb={kb} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("connection failed");
  fixture.list.mockResolvedValue([{ id: "doc-1", name: "guide.pdf" }]);
  fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  expect(await screen.findByRole("button", { name: "guide.pdf" })).toBeInTheDocument();
  expect(fixture.list).toHaveBeenLastCalledWith(kb.id, { force: true });
  expect(screen.queryByRole("button", { name: /Delete|Upload|Rename|Move|New folder/ })).not.toBeInTheDocument();
});

it("keeps remote names containing slashes flat and disables drag operations", async () => {
  fixture.list.mockResolvedValue([{ id: "doc-1", name: "产品/方案.pptx" }]);
  render(<KbFilesTab kb={kb} />);
  const document = await screen.findByRole("button", { name: "产品/方案.pptx" });
  expect(document.parentElement).toHaveAttribute("draggable", "false");
  fireEvent.click(document);
  expect(screen.getByTestId("preview").getAttribute("data-url")).toMatch(/doc-1$/);
  fireEvent.click(screen.getByRole("button", { name: "Collapse" }));
  expect(screen.getByRole("button", { name: "产品/方案.pptx" })).toHaveAttribute("aria-pressed", "true");
  fireEvent.click(screen.getByRole("button", { name: "Expand" }));
});

it("clears the preview when a refreshed remote document was removed", async () => {
  fixture.list.mockResolvedValue([{ id: "doc-1", name: "guide.pdf" }]);
  render(<KbFilesTab kb={kb} />);
  fireEvent.click(await screen.findByRole("button", { name: "guide.pdf" }));
  expect(screen.getByTestId("preview")).toBeInTheDocument();
  fixture.list.mockResolvedValue([]);
  fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  expect(await screen.findByText("No documents in this knowledge base.")).toBeInTheDocument();
  expect(screen.queryByTestId("preview")).not.toBeInTheDocument();
});

it("preserves local folder paths, management controls and preview URLs", async () => {
  fixture.list.mockResolvedValue([{ name: "folder", type: "folder" }, { name: "folder/guide.pdf", type: "file" }]);
  render(<KbFilesTab kb={{ ...kb, metadata: {} }} />);
  const document = await screen.findByRole("button", { name: "folder/guide.pdf" });
  expect(document.parentElement).toHaveAttribute("draggable", "true");
  expect(screen.getByRole("button", { name: "New folder" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Delete file" })).toBeInTheDocument();
  fireEvent.click(document);
  expect(screen.getByTestId("preview")).toHaveAttribute("data-url", "/api/knowledge-bases/workspace%3Ateam%3Akb%3Aremote/files/folder/guide.pdf");
  expect(screen.queryByRole("button", { name: "Parsed text" })).not.toBeInTheDocument();
});
