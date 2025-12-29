import React from "react";

export function NiceTable({ children }: { children: React.ReactNode }) {
  return (
    <div className="overflow-hidden rounded-xl border border-white/10">
      <table className="table-fixed w-full divide-y divide-white/10">
        <tbody className="divide-y divide-white/5 bg-white/5">{children}</tbody>
      </table>
    </div>
  );
}

export function FileRow({ left, right }: { left: React.ReactNode; right?: React.ReactNode }) {
  return (
    <tr className="hover:bg-white/5">
      <td className="px-3 py-2 align-middle text-sm text-white/90">{left}</td>
      <td className="px-3 py-2 align-middle text-right">{right}</td>
    </tr>
  );
}
