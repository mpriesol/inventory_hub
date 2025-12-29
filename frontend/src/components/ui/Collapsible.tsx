import React from "react";
export function Collapsible({title, children, defaultOpen=false}:{title:string;children:React.ReactNode;defaultOpen?:boolean}) {
  const [open, setOpen] = React.useState(defaultOpen);
  return (
    <div className="bg-slate-900/40 rounded-xl border border-white/10">
      <button onClick={()=>setOpen(o=>!o)} className="w-full text-left px-4 py-3 flex items-center justify-between">
        <span className="text-white/80">{title}</span>
        <span className="text-white/50 text-xs">{open ? "Hide" : "Show"}</span>
      </button>
      {open && <div className="p-4">{children}</div>}
    </div>
  );
}