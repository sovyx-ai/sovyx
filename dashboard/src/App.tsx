import { RouterProvider } from "react-router";
import { router } from "./router";
import { TokenEntryModal } from "./components/auth/token-entry-modal";
import { useAuth } from "./hooks/use-auth";

export default function App() {
  const { ready } = useAuth();

  return (
    <>
      <RouterProvider router={router} />
      <TokenEntryModal />
      {!ready && (
        <div className="fixed inset-0 z-40 bg-[var(--svx-color-bg-base)]/80 backdrop-blur-sm" />
      )}
    </>
  );
}
