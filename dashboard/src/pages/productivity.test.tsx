import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import ProductivityPage from "./productivity";
import { I18nextProvider } from "react-i18next";
import i18n from "@/lib/i18n";

describe("ProductivityPage", () => {
  it("renders coming soon placeholder", () => {
    render(
      <I18nextProvider i18n={i18n}>
        <ProductivityPage />
      </I18nextProvider>,
    );
    expect(screen.getByText(/coming soon/i)).toBeInTheDocument();
  });
});
