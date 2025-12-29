
import * as React from "react";

type JsonValue = Record<string, any>;

async function getJSON(url: string) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(await res.text());
  return await res.json();
}
async function sendJSON(url: string, method: "POST" | "PUT", body: any) {
  const res = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await res.text());
  return await res.json();
}

function tryFormat(obj: any) {
  try { return JSON.stringify(obj, null, 2); } catch { return ""; }
}
function tryParse(txt: string) {
  try { return JSON.parse(txt); } catch { return null; }
}

export default function ConfigAdminPanel() {
  const [tab, setTab] = React.useState<"console"|"shop"|"supplier">("console");
  const [shop, setShop] = React.useState("biketrek");
  const [supplier, setSupplier] = React.useState("paul-lange");

  const [text, setText] = React.useState<string>("");
  const [status, setStatus] = React.useState<string>("");

  const load = React.useCallback(async () => {
    try {
      setStatus("Loading...");
      let data: JsonValue = {};
      if (tab === "console") data = await getJSON("/configs/console");
      if (tab === "shop") data = await getJSON(`/shops/${shop}/config`);
      if (tab === "supplier") data = await getJSON(`/suppliers/${supplier}/config`);
      setText(tryFormat(data));
      setStatus("Loaded");
    } catch (e: any) {
      setStatus("Error: " + (e?.message || e));
    }
  }, [tab, shop, supplier]);

  const save = React.useCallback(async () => {
    try {
      const parsed = tryParse(text);
      if (!parsed) throw new Error("JSON is invalid.");
      setStatus("Saving...");
      let data: JsonValue = {};
      if (tab === "console") data = await sendJSON("/configs/console", "POST", parsed);
      if (tab === "shop") data = await sendJSON(`/shops/${shop}/config`, "PUT", parsed);
      if (tab === "supplier") data = await sendJSON(`/suppliers/${supplier}/config`, "PUT", parsed);
      setText(tryFormat(data));
      setStatus("Saved ✔");
    } catch (e: any) {
      setStatus("Error: " + (e?.message || e));
    }
  }, [tab, shop, supplier, text]);

  React.useEffect(() => { load(); }, [load]);

  return (
    <div className="p-4 space-y-3">
      <div className="flex items-center gap-2">
        <button className={`px-3 py-1 rounded ${tab==="console"?"bg-neutral-800 text-white":"bg-neutral-900 text-neutral-300"}`} onClick={()=>setTab("console")}>Console</button>
        <button className={`px-3 py-1 rounded ${tab==="shop"?"bg-neutral-800 text-white":"bg-neutral-900 text-neutral-300"}`} onClick={()=>setTab("shop")}>Shop</button>
        <button className={`px-3 py-1 rounded ${tab==="supplier"?"bg-neutral-800 text-white":"bg-neutral-900 text-neutral-300"}`} onClick={()=>setTab("supplier")}>Supplier</button>

        {tab==="shop" && (
          <input className="ml-3 px-2 py-1 bg-black border border-neutral-700 rounded text-sm"
                 value={shop} onChange={e=>setShop(e.target.value)} placeholder="shop code"/>
        )}
        {tab==="supplier" && (
          <input className="ml-3 px-2 py-1 bg-black border border-neutral-700 rounded text-sm"
                 value={supplier} onChange={e=>setSupplier(e.target.value)} placeholder="supplier code"/>
        )}

        <div className="ml-auto flex gap-2">
          <button className="px-3 py-1 rounded border border-neutral-700" onClick={load}>Reload</button>
          <button className="px-3 py-1 rounded bg-green-700 hover:bg-green-600" onClick={save}>Save JSON</button>
        </div>
      </div>

      <div className="text-xs text-neutral-400">{status}</div>

      <textarea
        className="w-full h-[70vh] bg-black text-neutral-100 font-mono text-sm p-3 rounded border border-neutral-800"
        value={text}
        onChange={e=>setText(e.target.value)}
        spellCheck={false}
      />
    </div>
  );
}
