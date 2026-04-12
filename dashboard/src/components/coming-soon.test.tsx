import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ComingSoon } from "./coming-soon";
import { HeartIcon } from "lucide-react";
import { I18nextProvider } from "react-i18next";
import i18n from "@/lib/i18n";

function renderWithI18n(ui: React.ReactElement) {
  return render(<I18nextProvider i18n={i18n}>{ui}</I18nextProvider>);
}

describe("ComingSoon", () => {
  it("renders title, badge, and description", () => {
    renderWithI18n(
      <ComingSoon
        icon={HeartIcon}
        titleKey="emotions.title"
        descriptionKey="emotions.description"
      />,
    );
    expect(screen.getByText(/emotions/i)).toBeInTheDocument();
    expect(screen.getByText(/coming soon/i)).toBeInTheDocument();
  });

  it("renders the provided icon", () => {
    const { container } = renderWithI18n(
      <ComingSoon
        icon={HeartIcon}
        titleKey="emotions.title"
        descriptionKey="emotions.description"
      />,
    );
    // Lucide renders an SVG
    expect(container.querySelector("svg")).toBeInTheDocument();
  });
});
