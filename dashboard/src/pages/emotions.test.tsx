import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import EmotionsPage from "./emotions";
import { I18nextProvider } from "react-i18next";
import { MemoryRouter } from "react-router";
import i18n from "@/lib/i18n";

describe("EmotionsPage", () => {
  it("renders emotions page with loading or empty state", () => {
    render(
      <MemoryRouter>
        <I18nextProvider i18n={i18n}>
          <EmotionsPage />
        </I18nextProvider>
      </MemoryRouter>,
    );
    expect(
      screen.getByText(/loading emotional data|emotional landscape|emotions/i),
    ).toBeInTheDocument();
  });
});
