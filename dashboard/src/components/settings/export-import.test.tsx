/**
 * ExportImportSection tests — TASK-304
 *
 * Covers: render, export click, import flow, confirmation dialog,
 * file validation, error states, loading states, GDPR badge.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@/test/test-utils";
import { ExportImportSection } from "./export-import";

/* ── Mock fetch globally ── */

const mockFetch = vi.fn();
globalThis.fetch = mockFetch;

/* ── Mock sonner toast ── */

const mockToastSuccess = vi.fn();
const mockToastError = vi.fn();

vi.mock("sonner", () => ({
  toast: {
    success: (...args: unknown[]) => mockToastSuccess(...args),
    error: (...args: unknown[]) => mockToastError(...args),
  },
}));

/* ── Mock URL + DOM APIs ── */

const mockCreateObjectURL = vi.fn(() => "blob:mock-url");
const mockRevokeObjectURL = vi.fn();
URL.createObjectURL = mockCreateObjectURL;
URL.revokeObjectURL = mockRevokeObjectURL;

beforeEach(() => {
  mockFetch.mockReset();
  mockToastSuccess.mockReset();
  mockToastError.mockReset();
  mockCreateObjectURL.mockClear();
  mockRevokeObjectURL.mockClear();
  localStorage.clear();
});

// ════════════════════════════════════════════════════════
// BASIC RENDERING
// ════════════════════════════════════════════════════════
describe("basic rendering", () => {
  it("renders section title", () => {
    render(<ExportImportSection />);
    expect(screen.getByText("Export / Import")).toBeInTheDocument();
  });

  it("renders export button", () => {
    render(<ExportImportSection />);
    expect(screen.getByText("Export Mind")).toBeInTheDocument();
  });

  it("renders import button", () => {
    render(<ExportImportSection />);
    expect(screen.getByText("Import Mind")).toBeInTheDocument();
  });

  it("renders GDPR badge", () => {
    render(<ExportImportSection />);
    expect(screen.getByText(/GDPR Art\. 20/)).toBeInTheDocument();
  });

  it("renders description text", () => {
    render(<ExportImportSection />);
    expect(screen.getByText(/Download or restore your mind data/)).toBeInTheDocument();
  });

  it("has hidden file input with .sovyx-mind accept", () => {
    render(<ExportImportSection />);
    const fileInput = screen.getByLabelText("Import Mind");
    expect(fileInput).toHaveAttribute("accept", ".sovyx-mind");
    expect(fileInput).toHaveClass("hidden");
  });
});

// ════════════════════════════════════════════════════════
// EXPORT FLOW
// ════════════════════════════════════════════════════════
describe("export flow", () => {
  it("calls fetch /api/export on export click", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      blob: () => Promise.resolve(new Blob(["test"])),
      headers: new Headers({ "content-disposition": 'attachment; filename="mind.sovyx-mind"' }),
    });

    render(<ExportImportSection />);
    fireEvent.click(screen.getByText("Export Mind"));

    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/export"),
        expect.any(Object),
      );
    });
  });

  it("shows success toast after export", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      blob: () => Promise.resolve(new Blob(["data"])),
      headers: new Headers({ "content-disposition": 'filename="test.sovyx-mind"' }),
    });

    render(<ExportImportSection />);
    fireEvent.click(screen.getByText("Export Mind"));

    await waitFor(() => {
      expect(mockToastSuccess).toHaveBeenCalled();
    });
  });

  it("shows error toast on export failure", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      text: () => Promise.resolve("Server error"),
    });

    render(<ExportImportSection />);
    fireEvent.click(screen.getByText("Export Mind"));

    await waitFor(() => {
      expect(mockToastError).toHaveBeenCalled();
    });
  });

  it("sends auth header when token exists", async () => {
    localStorage.setItem("sovyx_token", "test-token-123");
    mockFetch.mockResolvedValueOnce({
      ok: true,
      blob: () => Promise.resolve(new Blob(["data"])),
      headers: new Headers(),
    });

    render(<ExportImportSection />);
    fireEvent.click(screen.getByText("Export Mind"));

    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalledWith(
        expect.any(String),
        expect.objectContaining({
          headers: expect.objectContaining({ Authorization: "Bearer test-token-123" }),
        }),
      );
    });
  });

  it("handles network error on export", async () => {
    mockFetch.mockRejectedValueOnce(new Error("Network failure"));

    render(<ExportImportSection />);
    fireEvent.click(screen.getByText("Export Mind"));

    await waitFor(() => {
      expect(mockToastError).toHaveBeenCalled();
    });
  });
});

// ════════════════════════════════════════════════════════
// IMPORT FLOW
// ════════════════════════════════════════════════════════
describe("import flow", () => {
  it("rejects files without .sovyx-mind extension", async () => {
    render(<ExportImportSection />);
    const fileInput = screen.getByLabelText("Import Mind");

    const badFile = new File(["data"], "backup.zip", { type: "application/zip" });
    fireEvent.change(fileInput, { target: { files: [badFile] } });

    await waitFor(() => {
      expect(mockToastError).toHaveBeenCalled();
    });
    // No confirmation dialog should appear
    expect(screen.queryByText("Import Mind Data")).not.toBeInTheDocument();
  });

  it("shows confirmation dialog for valid .sovyx-mind file", async () => {
    render(<ExportImportSection />);
    const fileInput = screen.getByLabelText("Import Mind");

    const validFile = new File(["data"], "backup.sovyx-mind", { type: "application/octet-stream" });
    fireEvent.change(fileInput, { target: { files: [validFile] } });

    await waitFor(() => {
      expect(screen.getByText("Import Mind Data")).toBeInTheDocument();
    });
  });

  it("confirmation dialog shows filename", async () => {
    render(<ExportImportSection />);
    const fileInput = screen.getByLabelText("Import Mind");

    const validFile = new File(["data"], "my-backup.sovyx-mind");
    fireEvent.change(fileInput, { target: { files: [validFile] } });

    await waitFor(() => {
      expect(screen.getByText(/my-backup\.sovyx-mind/)).toBeInTheDocument();
    });
  });

  it("cancel button closes confirmation dialog", async () => {
    render(<ExportImportSection />);
    const fileInput = screen.getByLabelText("Import Mind");

    const validFile = new File(["data"], "backup.sovyx-mind");
    fireEvent.change(fileInput, { target: { files: [validFile] } });

    await waitFor(() => {
      expect(screen.getByText("Import Mind Data")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("Cancel"));

    await waitFor(() => {
      expect(screen.queryByText("Import Mind Data")).not.toBeInTheDocument();
    });
  });

  it("confirm import sends POST /api/import", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ ok: true, concepts_imported: 5, episodes_imported: 3 }),
    });

    render(<ExportImportSection />);
    const fileInput = screen.getByLabelText("Import Mind");

    const validFile = new File(["data"], "backup.sovyx-mind");
    fireEvent.change(fileInput, { target: { files: [validFile] } });

    await waitFor(() => {
      expect(screen.getByText("Import Mind Data")).toBeInTheDocument();
    });

    // Click the destructive "Import" button in the dialog (not the "Import Mind" button)
    const dialogButtons = screen.getAllByRole("button");
    const confirmBtn = dialogButtons.find((b) => b.textContent === "Import");
    fireEvent.click(confirmBtn!);

    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/import"),
        expect.objectContaining({ method: "POST" }),
      );
    });
  });

  it("shows success toast with import counts", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ ok: true, concepts_imported: 12, episodes_imported: 7 }),
    });

    render(<ExportImportSection />);
    const fileInput = screen.getByLabelText("Import Mind");

    const validFile = new File(["data"], "backup.sovyx-mind");
    fireEvent.change(fileInput, { target: { files: [validFile] } });

    await waitFor(() => screen.getByText("Import Mind Data"));
    const dialogButtons = screen.getAllByRole("button");
    const confirmBtn = dialogButtons.find((b) => b.textContent === "Import");
    fireEvent.click(confirmBtn!);

    await waitFor(() => {
      expect(mockToastSuccess).toHaveBeenCalled();
    });
  });

  it("shows error toast on import failure", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      json: () => Promise.resolve({ ok: false, error: "Corrupt file" }),
    });

    render(<ExportImportSection />);
    const fileInput = screen.getByLabelText("Import Mind");

    const validFile = new File(["data"], "backup.sovyx-mind");
    fireEvent.change(fileInput, { target: { files: [validFile] } });

    await waitFor(() => screen.getByText("Import Mind Data"));
    const dialogButtons = screen.getAllByRole("button");
    const confirmBtn = dialogButtons.find((b) => b.textContent === "Import");
    fireEvent.click(confirmBtn!);

    await waitFor(() => {
      expect(mockToastError).toHaveBeenCalled();
    });
  });
});

// ════════════════════════════════════════════════════════
// LOADING STATES
// ════════════════════════════════════════════════════════
describe("loading states", () => {
  it("disables buttons while exporting", async () => {
    // Never resolve to keep exporting state
    mockFetch.mockReturnValue(new Promise(() => {}));

    render(<ExportImportSection />);
    fireEvent.click(screen.getByText("Export Mind"));

    await waitFor(() => {
      const exportBtn = screen.getByText("Export Mind").closest("button");
      const importBtn = screen.getByText("Import Mind").closest("button");
      expect(exportBtn).toBeDisabled();
      expect(importBtn).toBeDisabled();
    });
  });
});
