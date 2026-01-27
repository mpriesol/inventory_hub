import React, { useRef, useState } from "react";
import { API_BASE } from "../../api/client";
import { PrimaryButton, OutlineButton } from "../../components/ui/button";

export function FeedUploader({
  supplier,
  onSuccess,
  onError,
}: {
  supplier: string;
  onSuccess?: (msg: string) => void;
  onError?: (msg: string) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);

  const pick = () => inputRef.current?.click();

  const upload = async () => {
    if (!file) return;
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const res = await fetch(`${API_BASE}/suppliers/${supplier}/feeds/upload`, {
        method: "POST",
        body: fd,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data?.detail || res.statusText);
      onSuccess?.(data?.message || "Feed uploaded");
    } catch (e: any) {
      onError?.(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex items-center gap-2">
      <input
        type="file"
        accept=".xml,text/xml,application/xml"
        ref={inputRef}
        className="hidden"
        onChange={(e) => setFile(e.target.files?.[0] || null)}
      />
      <OutlineButton onClick={pick}>Vybrať XML</OutlineButton>
      <span className="text-sm text-white/70 min-w-40 truncate">
        {file?.name ?? "Žiadny súbor nevybraný"}
      </span>
      <PrimaryButton disabled={!file || busy} onClick={upload}>
        Nahrať XML
      </PrimaryButton>
    </div>
  );
}
