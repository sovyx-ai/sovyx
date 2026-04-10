/**
 * Tests for ProviderConfig component.
 *
 * Covers: loading state, cloud providers, Ollama with models,
 * Ollama offline, save interaction, error state.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@/test/test-utils";
import { ProviderConfig } from "./provider-config";

// Mock the API module
vi.mock("@/lib/api", () => ({
  api: {
    get: vi.fn(),
    put: vi.fn(),
  },
}));

// Mock sonner toast
vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

import { api } from "@/lib/api";

const mockApi = api as unknown as {
  get: ReturnType<typeof vi.fn>;
  put: ReturnType<typeof vi.fn>;
};

function makeResponse(overrides: Record<string, unknown> = {}) {
  return {
    providers: [
      { name: "anthropic", configured: false, available: false },
      { name: "openai", configured: true, available: true },
      {
        name: "ollama",
        configured: true,
        available: true,
        reachable: true,
        models: ["llama3.1:latest", "mistral:7b"],
        base_url: "http://localhost:11434",
      },
    ],
    active: { provider: "openai", model: "gpt-4o", fast_model: "gpt-4o-mini" },
    ...overrides,
  };
}

describe("ProviderConfig", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows loading spinner initially", () => {
    mockApi.get.mockReturnValue(new Promise(() => {})); // never resolves
    render(<ProviderConfig />);
    // Loading state — spinner via animate-spin
    const spinner = document.querySelector(".animate-spin");
    expect(spinner).toBeInTheDocument();
  });

  it("renders cloud providers after load", async () => {
    mockApi.get.mockResolvedValue(makeResponse());
    render(<ProviderConfig />);

    await waitFor(() => {
      expect(screen.getByTestId("provider-openai")).toBeInTheDocument();
    });
    expect(screen.getByTestId("provider-anthropic")).toBeInTheDocument();
    expect(screen.getByText("configured")).toBeInTheDocument();
    expect(screen.getByText("not configured")).toBeInTheDocument();
  });

  it("renders Ollama with model dropdown when reachable", async () => {
    mockApi.get.mockResolvedValue(makeResponse());
    render(<ProviderConfig />);

    await waitFor(() => {
      expect(screen.getByTestId("provider-ollama")).toBeInTheDocument();
    });

    const select = screen.getByTestId("ollama-model-select");
    expect(select).toBeInTheDocument();

    // Models in dropdown
    const options = select.querySelectorAll("option:not([disabled])");
    expect(options).toHaveLength(2);
    expect(options[0]!.textContent).toBe("llama3.1:latest");
    expect(options[1]!.textContent).toBe("mistral:7b");
  });

  it("shows offline state when Ollama not reachable", async () => {
    const response = makeResponse({
      providers: [
        {
          name: "ollama",
          configured: true,
          available: false,
          reachable: false,
          models: [],
          base_url: "http://localhost:11434",
        },
      ],
    });
    mockApi.get.mockResolvedValue(response);
    render(<ProviderConfig />);

    await waitFor(() => {
      expect(screen.getByText("not running")).toBeInTheDocument();
    });
    // Ollama section shows install hint
    expect(screen.getByText(/ollama serve/)).toBeInTheDocument();
  });

  it("enables save button when selection changes", async () => {
    mockApi.get.mockResolvedValue(makeResponse());
    render(<ProviderConfig />);

    await waitFor(() => {
      expect(screen.getByTestId("ollama-model-select")).toBeInTheDocument();
    });

    // Initially save should be disabled (nothing changed)
    const saveBtn = screen.getByText("Save Changes");
    expect(saveBtn.closest("button")).toBeDisabled();

    // Select Ollama model
    fireEvent.change(screen.getByTestId("ollama-model-select"), {
      target: { value: "llama3.1:latest" },
    });

    // Now save should be enabled (provider changed from openai to ollama)
    expect(saveBtn.closest("button")).toBeEnabled();
  });

  it("calls PUT /api/providers on save", async () => {
    mockApi.get.mockResolvedValue(makeResponse());
    mockApi.put.mockResolvedValue({ ok: true });
    render(<ProviderConfig />);

    await waitFor(() => {
      expect(screen.getByTestId("ollama-model-select")).toBeInTheDocument();
    });

    // Change to Ollama
    fireEvent.change(screen.getByTestId("ollama-model-select"), {
      target: { value: "llama3.1:latest" },
    });

    fireEvent.click(screen.getByText("Save Changes"));

    await waitFor(() => {
      expect(mockApi.put).toHaveBeenCalledWith("/api/providers", {
        provider: "ollama",
        model: "llama3.1:latest",
      });
    });
  });

  it("shows error state on load failure", async () => {
    mockApi.get.mockRejectedValue(new Error("Network error"));
    render(<ProviderConfig />);

    await waitFor(() => {
      expect(screen.getByText(/Failed to load providers/)).toBeInTheDocument();
    });

    // Retry button present
    expect(screen.getByText("Retry")).toBeInTheDocument();
  });

  it("shows active provider summary", async () => {
    mockApi.get.mockResolvedValue(makeResponse());
    render(<ProviderConfig />);

    await waitFor(() => {
      expect(screen.getByText(/Active/)).toBeInTheDocument();
    });
    // Active line: "Active: OpenAI / gpt-4o"
    expect(screen.getByText(/OpenAI \/ gpt-4o/)).toBeInTheDocument();
  });

  it("disables not-configured cloud providers", async () => {
    mockApi.get.mockResolvedValue(makeResponse());
    render(<ProviderConfig />);

    await waitFor(() => {
      expect(screen.getByTestId("provider-anthropic")).toBeInTheDocument();
    });

    const anthropicBtn = screen.getByTestId("provider-anthropic");
    expect(anthropicBtn).toBeDisabled();
  });
});

  it("shows empty state when no providers available", async () => {
    const response = makeResponse({
      providers: [
        { name: "anthropic", configured: false, available: false },
        { name: "openai", configured: false, available: false },
        {
          name: "ollama",
          configured: true,
          available: false,
          reachable: false,
          models: [],
          base_url: "http://localhost:11434",
        },
      ],
      active: { provider: "", model: "", fast_model: "" },
    });
    mockApi.get.mockResolvedValue(response);
    render(<ProviderConfig />);

    await waitFor(() => {
      expect(screen.getByTestId("provider-empty-state")).toBeInTheDocument();
    });
    expect(screen.getByText("No provider configured")).toBeInTheDocument();
    expect(screen.getByText(/ANTHROPIC_API_KEY/)).toBeInTheDocument();
    expect(screen.getByText(/ollama pull/)).toBeInTheDocument();
  });
