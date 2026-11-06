import React from "react";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AddMaterialsDialog } from "@/components/reading/library/AddMaterialsDialog";

const mock = vi.hoisted(() => ({
  addMaterial: vi.fn(),
  checkDuplicates: vi.fn(),
  createWorkspace: vi.fn(),
  importUrls: vi.fn(),
  listLibrary: vi.fn(),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock("@/lib/reading-api", () => ({
  uploadMaterial: vi.fn(),
}));

vi.mock("@/lib/reading-workspace-api", () => ({
  addReadingWorkspaceMaterial: mock.addMaterial,
  checkReadingDuplicates: mock.checkDuplicates,
  createReadingWorkspace: mock.createWorkspace,
  importReadingUrls: mock.importUrls,
  listReadingLibraryMaterials: mock.listLibrary,
  readingContentId: vi.fn(),
}));

beforeEach(() => {
  vi.clearAllMocks();
  mock.checkDuplicates.mockResolvedValue([]);
  mock.importUrls.mockResolvedValue({
    workspace: { workspace_id: "workspace-1" },
  });
});

describe("Reading Add Materials link entry", () => {
  it("does not offer YouTube or Invidious imports", () => {
    render(
      <AddMaterialsDialog
        mode="add"
        workspaceId="workspace-1"
        onClose={vi.fn()}
        onDone={vi.fn()}
      />,
    );

    expect(
      screen.queryByRole("button", { name: "Browse Invidious" }),
    ).toBeNull();
    expect(
      screen.getByPlaceholderText("or paste a link — web page or Bilibili"),
    ).toBeVisible();
  });
});
