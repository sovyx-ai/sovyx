/**
 * ExportImportSection — Functional Export / Import for Mind data.
 *
 * - Export: Downloads .sovyx-mind ZIP via GET /api/export
 * - Import: Uploads .sovyx-mind ZIP via POST /api/import (multipart)
 * - Confirmation dialog before import (destructive action)
 * - Loading states, error handling, toast feedback
 *
 * Ref: SPE-028 §5–5B, GDPR Art. 20
 */

import { useState, useRef, useCallback } from "react";
import { useTranslation } from "react-i18next";
import {
  DownloadIcon,
  UploadIcon,
  Loader2Icon,
  ShieldCheckIcon,
} from "lucide-react";
import { toast } from "sonner";
import { apiFetch } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export function ExportImportSection() {
  const { t } = useTranslation(["settings", "common"]);
  const [exporting, setExporting] = useState(false);
  const [importing, setImporting] = useState(false);
  const [showConfirm, setShowConfirm] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const pendingFile = useRef<File | null>(null);

  const handleExport = useCallback(async () => {
    setExporting(true);
    try {
      const res = await apiFetch("/api/export");

      if (!res.ok) {
        const body = await res.text().catch(() => "Export failed");
        throw new Error(body);
      }

      const blob = await res.blob();
      const disposition = res.headers.get("content-disposition");
      const filenameMatch = disposition?.match(/filename="?([^"]+)"?/);
      const filename = filenameMatch?.[1] ?? "mind.sovyx-mind";

      // Trigger browser download
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      URL.revokeObjectURL(url);
      a.remove();

      toast.success(t("exportImport.exportSuccess"));
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Export failed";
      toast.error(t("exportImport.exportFailed", { error: msg }));
    } finally {
      setExporting(false);
    }
  }, [t]);

  const handleFileSelect = useCallback(() => {
    fileRef.current?.click();
  }, []);

  const handleFileChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (!file) return;

      // Validate file extension
      if (!file.name.endsWith(".sovyx-mind")) {
        toast.error(t("exportImport.invalidFile"));
        return;
      }

      pendingFile.current = file;
      setShowConfirm(true);

      // Reset input so the same file can be selected again
      e.target.value = "";
    },
    [t],
  );

  const handleImportConfirm = useCallback(async () => {
    const file = pendingFile.current;
    if (!file) return;

    setShowConfirm(false);
    setImporting(true);

    try {
      const form = new FormData();
      form.append("file", file);

      const res = await apiFetch("/api/import", {
        method: "POST",
        body: form,
      });

      const data = await res.json();

      if (!res.ok || !data.ok) {
        throw new Error(data.error ?? "Import failed");
      }

      toast.success(
        t("exportImport.importSuccess", {
          concepts: data.concepts_imported ?? 0,
          episodes: data.episodes_imported ?? 0,
        }),
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Import failed";
      toast.error(t("exportImport.importFailed", { error: msg }));
    } finally {
      setImporting(false);
      pendingFile.current = null;
    }
  }, [t]);

  return (
    <>
      <section className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4">
        <div className="flex items-center gap-2">
          <DownloadIcon className="size-4 text-[var(--svx-color-brand-primary)]" />
          <h2 className="text-sm font-medium text-[var(--svx-color-text-primary)]">
            {t("exportImport.title")}
          </h2>
        </div>
        <p className="mt-1 text-xs text-[var(--svx-color-text-tertiary)]">
          {t("exportImport.description")}
        </p>

        <div className="mt-4 flex flex-wrap gap-3">
          {/* Export */}
          <Button
            size="sm"
            variant="outline"
            onClick={handleExport}
            disabled={exporting || importing}
          >
            {exporting ? (
              <Loader2Icon className="size-4 animate-spin" />
            ) : (
              <DownloadIcon className="size-4" />
            )}
            {t("exportImport.exportButton")}
          </Button>

          {/* Import */}
          <Button
            size="sm"
            variant="outline"
            onClick={handleFileSelect}
            disabled={exporting || importing}
          >
            {importing ? (
              <Loader2Icon className="size-4 animate-spin" />
            ) : (
              <UploadIcon className="size-4" />
            )}
            {t("exportImport.importButton")}
          </Button>

          <input
            ref={fileRef}
            type="file"
            accept=".sovyx-mind"
            onChange={handleFileChange}
            className="hidden"
            aria-label={t("exportImport.importButton")}
          />
        </div>

        {/* GDPR badge */}
        <div className="mt-3 flex items-center gap-1.5 text-[10px] text-[var(--svx-color-text-disabled)]">
          <ShieldCheckIcon className="size-3" />
          {t("exportImport.gdprNote")}
        </div>
      </section>

      {/* Import confirmation dialog */}
      <Dialog open={showConfirm} onOpenChange={setShowConfirm}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("exportImport.confirmTitle")}</DialogTitle>
            <DialogDescription>
              {t("exportImport.confirmDescription", {
                filename: pendingFile.current?.name ?? "",
              })}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setShowConfirm(false)}>
              {t("common:actions.cancel")}
            </Button>
            <Button variant="destructive" onClick={handleImportConfirm}>
              {t("exportImport.confirmImport")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
