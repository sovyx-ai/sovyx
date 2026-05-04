import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./lib/i18n"; // Initialize i18n before App
import { applyLocaleDetection } from "./lib/i18n-detect";
import "./index.css";
import App from "./App.tsx";

// First-visit auto-detect: read localStorage choice OR sniff
// navigator.language. Mission v0.30.3 §T3.4 (D6).
applyLocaleDetection();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
