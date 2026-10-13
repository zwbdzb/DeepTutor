import ExcelJS from "exceljs";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import XlsxPreview from "@/components/chat/preview/previewers/XlsxPreview";

const fixture = vi.hoisted(() => ({ apiFetch: vi.fn() }));

vi.mock("@/lib/api", () => ({ apiFetch: fixture.apiFetch }));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

async function serveWorkbook(workbook: ExcelJS.Workbook) {
  const bytes = await workbook.xlsx.writeBuffer();
  // Fetch supplies an ArrayBuffer, unlike ExcelJS's Node Buffer output.
  const buffer = Uint8Array.from(new Uint8Array(bytes)).buffer;
  fixture.apiFetch.mockResolvedValue({
    ok: true,
    headers: new Headers(),
    arrayBuffer: async () => buffer,
  });
}

it("renders a workbook with empty merged cells and can switch worksheets", async () => {
  const workbook = new ExcelJS.Workbook();
  const sheet = workbook.addWorksheet("Weekly report");
  sheet.getCell("A1").value = "Report title";
  sheet.mergeCells("B2:C2");
  workbook.addWorksheet("Next week").getCell("A1").value = "Next week's plan";
  await serveWorkbook(workbook);

  render(<XlsxPreview url="/api/knowledge-bases/remote/remote-file-preview/report" />);

  expect(await screen.findByText("Report title")).toBeInTheDocument();
  const rows = screen.getAllByRole("row");
  expect(rows).toHaveLength(2);
  const merged = rows[1].querySelectorAll("td")[2];
  expect(merged.textContent).toBe("");
  expect(merged).toHaveAttribute("colspan", "2");
  expect(rows[1].querySelectorAll("td")).toHaveLength(3);
  await userEvent.click(screen.getByRole("button", { name: "Next week" }));
  expect(await screen.findByText("Next week's plan")).toBeInTheDocument();
  expect(screen.queryByText("Couldn't render this spreadsheet — use Download to open it.")).not.toBeInTheDocument();
});

it("uses the master cell's text for merged formulas and rich text", async () => {
  const workbook = new ExcelJS.Workbook();
  const sheet = workbook.addWorksheet("Values");
  sheet.getCell("A1").value = { formula: "1+1", result: 2 };
  sheet.mergeCells("A1:B1");
  sheet.getCell("A2").value = { richText: [{ text: "Formatted title" }] };
  sheet.mergeCells("A2:B2");
  await serveWorkbook(workbook);

  render(<XlsxPreview url="/report.xlsx" />);

  expect(await screen.findAllByText("Formatted title")).toHaveLength(1);
  const cells = screen.getAllByRole("row")[0].querySelectorAll("td");
  expect(cells[1]).toHaveTextContent("2");
  expect(cells[1]).toHaveAttribute("colspan", "2");
  expect(cells).toHaveLength(2);
});

it("renders rectangular and vertical merges once without hiding neighboring cells", async () => {
  const workbook = new ExcelJS.Workbook();
  const sheet = workbook.addWorksheet("Merges");
  sheet.getCell("A1").value = "Section heading";
  sheet.mergeCells("A1:C2");
  sheet.getCell("D1").value = "Top neighbor";
  sheet.getCell("D2").value = "Bottom neighbor";
  sheet.getCell("A3").value = "Vertical label";
  sheet.mergeCells("A3:A4");
  sheet.getCell("B4").value = "Last row";
  await serveWorkbook(workbook);

  render(<XlsxPreview url="/merges.xlsx" />);

  const heading = await screen.findByText("Section heading");
  expect(screen.getAllByText("Section heading")).toHaveLength(1);
  expect(heading).toHaveAttribute("colspan", "3");
  expect(heading).toHaveAttribute("rowspan", "2");
  expect(screen.getByText("Vertical label")).toHaveAttribute("rowspan", "2");
  expect(screen.getByText("Top neighbor")).toBeInTheDocument();
  expect(screen.getByText("Bottom neighbor")).toBeInTheDocument();
  expect(screen.getByText("Last row")).toBeInTheDocument();
  const rows = screen.getAllByRole("row");
  expect(rows[1].querySelectorAll("td")).toHaveLength(2);
  expect(rows[3].querySelectorAll("td")[0]).toHaveTextContent("4");
});

it("clips merges at the row and column preview limits", async () => {
  const workbook = new ExcelJS.Workbook();
  const sheet = workbook.addWorksheet("Many rows");
  sheet.getCell("A999").value = "Clipped rows";
  sheet.mergeCells("A999:A1002");
  sheet.getCell("A1003").value = "Outside preview";
  const columns = workbook.addWorksheet("Many columns");
  columns.getCell("BG1").value = "Clipped columns";
  columns.mergeCells("BG1:BJ1");
  columns.getCell("BK1").value = "Outside preview";
  await serveWorkbook(workbook);

  render(<XlsxPreview url="/large.xlsx" />);

  const merged = await screen.findByText("Clipped rows");
  expect(screen.getAllByText("Clipped rows")).toHaveLength(1);
  expect(merged).toHaveAttribute("rowspan", "2");
  expect(screen.queryByText("Outside preview")).not.toBeInTheDocument();
  expect(screen.getByText("Large sheet — preview truncated. Download for the full file.")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Many columns" }));
  expect(await screen.findByText("Clipped columns")).toHaveAttribute("colspan", "2");
  expect(screen.queryByText("Outside preview")).not.toBeInTheDocument();
});
