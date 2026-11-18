/**
 * The persona gallery's two affordances:
 *   - category tabs (全部 / 教育 / 办公 / IT编程 / 设计 / 多媒体 / 营销) above
 *     the card grid;
 *   - the 「召唤」button in each card's bottom-right corner, which hands the
 *     persona to the chat page through `?persona=`.
 *
 * The list deliberately has no `code` persona so the "empty category" copy is
 * exercised too, and one persona with no category at all (the shape every
 * user-authored persona has) so "All" is proven to include the ungrouped.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";

const { PERSONAS, getPersona, push } = vi.hoisted(() => ({
  PERSONAS: [
    {
      name: "ket-strategist",
      display_name: "KET 备考策略师",
      description: "KET 备考策略",
      category: "education",
      source: "user",
      read_only: false,
    },
    {
      name: "cv-auditor",
      display_name: "简历审计师",
      description: "简历诊断",
      category: "work",
      source: "user",
      read_only: false,
    },
    {
      name: "my-own",
      display_name: "",
      description: "手写人格",
      category: "",
      source: "user",
      read_only: false,
    },
  ],
  getPersona: vi.fn(),
  push: vi.fn(),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn(), prefetch: vi.fn(), back: vi.fn() }),
  usePathname: () => "/space/personas",
}));

vi.mock("@/lib/personas-api", () => ({
  listPersonas: async () => PERSONAS,
  getPersona,
  createPersona: vi.fn(),
  updatePersona: vi.fn(),
  deletePersona: vi.fn(),
}));

import PersonasSection from "@/components/space/PersonasSection";

/** The tab whose label starts with `label` (its accessible name also has a count). */
function tab(label: string): HTMLElement {
  return screen.getByRole("button", { name: new RegExp(`^${label}`) });
}

it("narrows the grid to one category and keeps every persona under All", async () => {
  render(<PersonasSection />);

  // "All" is the initial tab: every persona is visible, grouped or not.
  await screen.findByText("KET 备考策略师");
  expect(screen.getByText("简历审计师")).toBeInTheDocument();
  expect(screen.getByText("my-own")).toBeInTheDocument();
  expect(tab("persona.category.all")).toHaveAttribute("aria-pressed", "true");

  fireEvent.click(tab("persona.category.education"));
  await waitFor(() => expect(screen.queryByText("简历审计师")).not.toBeInTheDocument());
  expect(screen.getByText("KET 备考策略师")).toBeInTheDocument();
  // An uncategorised persona belongs to no filtered tab.
  expect(screen.queryByText("my-own")).not.toBeInTheDocument();
  expect(tab("persona.category.education")).toHaveAttribute("aria-pressed", "true");

  fireEvent.click(tab("persona.category.work"));
  await waitFor(() => expect(screen.getByText("简历审计师")).toBeInTheDocument());
  expect(screen.queryByText("KET 备考策略师")).not.toBeInTheDocument();

  // A category with no persona says so instead of rendering an empty grid.
  fireEvent.click(tab("persona.category.code"));
  await waitFor(() =>
    expect(screen.getByText("No personas in this category yet.")).toBeInTheDocument(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Show all" }));
  await waitFor(() => expect(screen.getByText("KET 备考策略师")).toBeInTheDocument());
  expect(screen.getByText("my-own")).toBeInTheDocument();
});

it("summons a persona into the chat page without opening the card", async () => {
  render(<PersonasSection />);
  await screen.findByText("简历审计师");

  fireEvent.click(
    screen.getByRole("button", { name: "Summon persona: 简历审计师" }),
  );

  expect(push).toHaveBeenCalledWith("/chat?persona=cv-auditor");
  // stopPropagation: the card's click handler (which opens the viewer) must not fire.
  expect(getPersona).not.toHaveBeenCalled();
});
