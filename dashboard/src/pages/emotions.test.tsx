import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import EmotionsPage from "./emotions";
import { I18nextProvider } from "react-i18next";
import i18n from "@/lib/i18n";

describe("EmotionsPage", () => {
  it("renders coming soon placeholder", () => {
    render(
      <I18nextProvider i18n={i18n}>
        <EmotionsPage />
      </I18nextProvider>,
    );
    expect(screen.getByText(/coming soon/i)).toBeInTheDocument();
  });
});
