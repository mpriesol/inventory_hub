import React, { useState } from "react";
import LogsModal from "./LogsModal";

export default function GlobalLogsButton() {
  const [open, setOpen] = useState(false);
  const btn =
    "inline-flex items-center rounded-2xl px-3 py-1.5 text-sm font-medium border border-white/20 text-white hover:bg-white/10 transition";
  return (
    <>
      <button className={btn} onClick={() => setOpen(true)} title="View last logs">
        View last logs
      </button>
      <LogsModal open={open} onClose={() => setOpen(false)} />
    </>
  );
}
