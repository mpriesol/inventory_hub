import React from "react";

export function Section({ title, right, children }: { title: string; right?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="bg-white/5 rounded-2xl border border-white/10 shadow-sm p-4 md:p-6">
      <div className="flex items-center justify-between gap-4 mb-4">
        <h2 className="text-lg md:text-xl font-semibold tracking-tight text-white">{title}</h2>
        {right}
      </div>
      {children}
    </section>
  );
}
