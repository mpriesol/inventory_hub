import React, { useEffect, useMemo, useState } from 'react'

// --- Config ---------------------------------------------------------------
const API_BASE = (import.meta as any).env?.VITE_API_BASE ?? ''

// --- Types ----------------------------------------------------------------
interface SupplierOut {
  name: string
  adapter: string
  base_path: string
  supplier_code: string
  config_json?: Record<string, unknown>
}

interface FilesResponse {
  files: string[] // most likely returned as "data/<relpath>" strings
}

interface PreviewResponse {
  columns: string[]
  rows: (string | number)[][]
  total_columns: number
  preview_rows: number
}

interface PrepareRunResponse {
  run_id: string
  outputs: { existing?: string | null; new?: string | null; unmatched?: string | null }
  stats?: { existing_rows?: number; new_rows?: number; unmatched_rows?: number }
  log?: string
}

// --- Helpers --------------------------------------------------------------
const j = async <T,>(res: Response) => {
  if (!res.ok) throw new Error(await res.text())
  return (await res.json()) as T
}

const api = {
  suppliers: async () => j<SupplierOut[]>(await fetch(`${API_BASE}/suppliers`)),
  files: async (supplier: string, area: string, monthsBack = 3) =>
    j<FilesResponse>(await fetch(`${API_BASE}/suppliers/${supplier}/files?area=${encodeURIComponent(area)}&months_back=${monthsBack}`)),
  preview: async (relpath: string, maxRows = 50) =>
    j<PreviewResponse>(await fetch(`${API_BASE}/files/preview?relpath=${encodeURIComponent(relpath)}&max_rows=${maxRows}`)),
  refreshFeed: async (supplier: string, source_url?: string) =>
    j<{ raw_path: string; converted_csv: string }>(
      await fetch(`${API_BASE}/suppliers/${supplier}/feeds/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(source_url ? { source_url } : {}),
      })
    ),
  prepareRun: async (body: Record<string, unknown>) =>
    j<PrepareRunResponse>(
      await fetch(`${API_BASE}/runs/prepare`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
    ),
}

const isDataPrefixed = (p: string) => p.startsWith('data/')
const toRel = (p: string) => (isDataPrefixed(p) ? p.replace(/^data\//, '') : p)
const toHref = (p?: string | null) => (p ? `${API_BASE}/${p}` : undefined)

// Basic badge
function Badge(props: { children: React.ReactNode; tone?: 'ok' | 'warn' | 'muted' }) {
  const map: Record<string, string> = {
    ok: 'bg-emerald-100 text-emerald-700 border-emerald-200',
    warn: 'bg-amber-100 text-amber-800 border-amber-200',
    muted: 'bg-slate-100 text-slate-700 border-slate-200',
  }
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium border ${map[props.tone ?? 'muted']}`}>
      {props.children}
    </span>
  )
}

// Small table component
function TinyTable({ columns, rows }: PreviewResponse) {
  return (
    <div className="overflow-auto border rounded-xl">
      <table className="min-w-full text-sm">
        <thead>
          <tr className="bg-slate-50">
            {columns.map((c) => (
              <th key={c} className="px-3 py-2 text-left font-semibold text-slate-700 whitespace-nowrap">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, idx) => (
            <tr key={idx} className="odd:bg-white even:bg-slate-50">
              {r.map((cell, i) => (
                <td key={i} className="px-3 py-2 align-top whitespace-nowrap max-w-[28rem] truncate" title={String(cell ?? '')}>
                  {String(cell ?? '')}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function App() {
  const [suppliers, setSuppliers] = useState<SupplierOut[]>([])
  const [supplier, setSupplier] = useState<string>('')

  const [invoices, setInvoices] = useState<string[]>([])
  const [feedsConverted, setFeedsConverted] = useState<string[]>([])
  const [importsUpgates, setImportsUpgates] = useState<string[]>([])

  const [loading, setLoading] = useState<string>('')
  const [error, setError] = useState<string>('')

  const [sourceUrl, setSourceUrl] = useState<string>('') // optional override for refresh
  const [shopRef, setShopRef] = useState<string>('biketrek')

  const [selectedInvoice, setSelectedInvoice] = useState<string>('')
  const [prepareResult, setPrepareResult] = useState<PrepareRunResponse | null>(null)

  const [previewRel, setPreviewRel] = useState<string>('')
  const [preview, setPreview] = useState<PreviewResponse | null>(null)

  const canPrepare = useMemo(() => supplier && selectedInvoice && shopRef, [supplier, selectedInvoice, shopRef])

  useEffect(() => {
    ;(async () => {
      try {
        setLoading('suppliers')
        const data = await api.suppliers()
        setSuppliers(data)
        if (data.length && !supplier) setSupplier(data[0].supplier_code)
      } catch (e: any) {
        setError(e.message || String(e))
      } finally {
        setLoading('')
      }
    })()
  }, [])

  useEffect(() => {
    if (!supplier) return
    ;(async () => {
      try {
        setLoading('files')
        const inv = await api.files(supplier, 'invoices_csv')
        const conv = await api.files(supplier, 'feeds_converted')
        const imp = await api.files(supplier, 'imports_upgates')
        setInvoices(inv.files)
        setFeedsConverted(conv.files)
        setImportsUpgates(imp.files)
      } catch (e: any) {
        setError(e.message || String(e))
      } finally {
        setLoading('')
      }
    })()
  }, [supplier])

  const doRefresh = async () => {
    if (!supplier) return
    try {
      setError(''); setLoading('refresh')
      const { converted_csv } = await api.refreshFeed(supplier, sourceUrl || undefined)
      // reload list after refresh
      const conv = await api.files(supplier, 'feeds_converted')
      setFeedsConverted(conv.files)
      // auto-preview the freshly created CSV
      const rel = toRel(converted_csv)
      setPreviewRel(rel)
      setPreview(await api.preview(rel, 25))
    } catch (e: any) {
      setError(e.message || String(e))
    } finally {
      setLoading('')
    }
  }

  const openPreview = async (dataPathOrRel: string) => {
    try {
      setError(''); setLoading('preview')
      const rel = toRel(dataPathOrRel)
      setPreviewRel(rel)
      setPreview(await api.preview(rel, 25))
    } catch (e: any) {
      setError(e.message || String(e))
    } finally {
      setLoading('')
    }
  }

  const doPrepare = async () => {
    if (!canPrepare) return
    try {
      setError(''); setLoading('prepare')
      const body = {
        supplier_ref: supplier,
        shop_ref: shopRef,
        invoice_relpath: toRel(selectedInvoice), // API expects path relative to INVENTORY_DATA_ROOT
        months_back: 1,
        upgates_csv_override: null,
      }
      const res = await api.prepareRun(body)
      setPrepareResult(res)
      // refresh list of generated imports
      const imp = await api.files(supplier, 'imports_upgates')
      setImportsUpgates(imp.files)
    } catch (e: any) {
      setError(e.message || String(e))
    } finally {
      setLoading('')
    }
  }

  return (
    <div className="min-h-dvh bg-slate-50 text-slate-900">
      <div className="max-w-7xl mx-auto p-6 space-y-6">
        <header className="flex items-center justify-between">
          <h1 className="text-2xl font-bold">Supplier Console</h1>
          <div className="text-sm text-slate-500">API: {API_BASE || '(same-origin)'}</div>
        </header>

        {/* Controls */}
        <section className="grid md:grid-cols-2 gap-4">
          <div className="bg-white border rounded-2xl p-4 space-y-3">
            <div className="flex items-center gap-3">
              <label className="text-sm w-28">Supplier</label>
              <select
                className="flex-1 border rounded-lg px-3 py-2"
                value={supplier}
                onChange={(e) => setSupplier(e.target.value)}
              >
                {suppliers.map((s) => (
                  <option key={s.supplier_code} value={s.supplier_code}>
                    {s.name} ({s.supplier_code})
                  </option>
                ))}
              </select>
            </div>

            <div className="flex items-center gap-3">
              <label className="text-sm w-28">Shop</label>
              <input
                className="flex-1 border rounded-lg px-3 py-2"
                placeholder="biketrek"
                value={shopRef}
                onChange={(e) => setShopRef(e.target.value)}
              />
            </div>

            <div className="flex items-center gap-3">
              <label className="text-sm w-28">Feed source</label>
              <input
                className="flex-1 border rounded-lg px-3 py-2"
                placeholder="(optional) local XML path or leave empty to use config"
                value={sourceUrl}
                onChange={(e) => setSourceUrl(e.target.value)}
              />
            </div>

            <div className="flex items-center gap-3">
              <button
                className="px-4 py-2 rounded-lg bg-slate-900 text-white hover:bg-slate-800 disabled:opacity-50"
                onClick={doRefresh}
                disabled={!supplier || loading === 'refresh'}
              >
                {loading === 'refresh' ? 'Refreshing…' : 'Refresh Feed'}
              </button>
              {loading && <Badge tone="muted">{loading}</Badge>}
            </div>

            {error && (
              <div className="text-sm text-red-600 border border-red-200 rounded-lg p-2 bg-red-50">{error}</div>
            )}
          </div>

          <div className="bg-white border rounded-2xl p-4 space-y-3">
            <div className="font-semibold">Prepare Run</div>
            <div className="text-sm text-slate-600">1) Vyber faktúru zľava • 2) Zadaj shop • 3) Spusť</div>
            <div className="flex items-center gap-3">
              <div className="text-sm w-28">Invoice</div>
              <input className="flex-1 border rounded-lg px-3 py-2" value={selectedInvoice ? toRel(selectedInvoice) : ''} readOnly />
            </div>
            <div className="flex items-center gap-3">
              <button
                className="px-4 py-2 rounded-lg bg-emerald-600 text-white hover:bg-emerald-500 disabled:opacity-50"
                onClick={doPrepare}
                disabled={!canPrepare || loading === 'prepare'}
              >
                {loading === 'prepare' ? 'Preparing…' : 'Run / Prepare'}
              </button>
              {prepareResult?.stats && (
                <Badge tone="ok">
                  existing {prepareResult.stats.existing_rows ?? 0} • new {prepareResult.stats.new_rows ?? 0} • unmatched {prepareResult.stats.unmatched_rows ?? 0}
                </Badge>
              )}
            </div>
            {prepareResult?.outputs && (
              <div className="text-sm grid gap-1">
                {prepareResult.outputs.existing && (
                  <a className="text-emerald-700 hover:underline" href={toHref(prepareResult.outputs.existing)} target="_blank">Existing CSV</a>
                )}
                {prepareResult.outputs.new && (
                  <a className="text-emerald-700 hover:underline" href={toHref(prepareResult.outputs.new)} target="_blank">New CSV</a>
                )}
                {prepareResult.outputs.unmatched && (
                  <a className="text-emerald-700 hover:underline" href={toHref(prepareResult.outputs.unmatched)} target="_blank">Unmatched CSV</a>
                )}
              </div>
            )}
          </div>
        </section>

        {/* Panels */}
        <section className="grid lg:grid-cols-2 gap-4">
          <div className="bg-white border rounded-2xl p-4 space-y-3">
            <div className="flex items-center justify-between">
              <div className="font-semibold">Invoices (CSV)</div>
              <Badge tone="muted">{invoices.length}</Badge>
            </div>
            <div className="h-64 overflow-auto border rounded-xl divide-y">
              {invoices.map((p) => (
                <div key={p} className="flex items-center gap-3 px-3 py-2">
                  <input
                    type="radio"
                    name="invoice"
                    checked={selectedInvoice === p}
                    onChange={() => setSelectedInvoice(p)}
                  />
                  <a className="text-slate-800 hover:underline" href={toHref(p)} target="_blank" rel="noreferrer">
                    {toRel(p)}
                  </a>
                  <button className="ml-auto text-xs text-slate-600 hover:text-slate-900" onClick={() => openPreview(p)}>preview</button>
                </div>
              ))}
            </div>
          </div>

          <div className="bg-white border rounded-2xl p-4 space-y-3">
            <div className="flex items-center justify-between">
              <div className="font-semibold">Feeds / Converted</div>
              <Badge tone="muted">{feedsConverted.length}</Badge>
            </div>
            <div className="h-64 overflow-auto border rounded-xl divide-y">
              {feedsConverted.map((p) => (
                <div key={p} className="flex items-center gap-3 px-3 py-2">
                  <a className="text-slate-800 hover:underline" href={toHref(p)} target="_blank" rel="noreferrer">
                    {toRel(p)}
                  </a>
                  <button className="ml-auto text-xs text-slate-600 hover:text-slate-900" onClick={() => openPreview(p)}>preview</button>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className="bg-white border rounded-2xl p-4 space-y-3">
          <div className="flex items-center justify-between">
            <div className="font-semibold">Imports / Upgates</div>
            <Badge tone="muted">{importsUpgates.length}</Badge>
          </div>
          <div className="h-64 overflow-auto border rounded-xl divide-y">
            {importsUpgates.map((p) => (
              <div key={p} className="flex items-center gap-3 px-3 py-2">
                <a className="text-slate-800 hover:underline" href={toHref(p)} target="_blank" rel="noreferrer">
                  {toRel(p)}
                </a>
                <button className="ml-auto text-xs text-slate-600 hover:text-slate-900" onClick={() => openPreview(p)}>preview</button>
              </div>
            ))}
          </div>
        </section>

        {/* Preview */}
        {preview && (
          <section className="bg-white border rounded-2xl p-4 space-y-3">
            <div className="flex items-center gap-3">
              <div className="font-semibold">Preview</div>
              <Badge tone="muted">{preview.preview_rows} rows</Badge>
              <div className="text-xs text-slate-500">{previewRel}</div>
            </div>
            <TinyTable {...preview} />
          </section>
        )}
      </div>
    </div>
  )
}
