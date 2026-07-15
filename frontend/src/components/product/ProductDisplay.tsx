// src/components/product/ProductDisplay.tsx
//
// Modulárne komponenty na zobrazovanie produktov, použiteľné kdekoľvek
// (sklad, naskladňovanie, budúce obrazovky):
//
//   <ProductThumb url name size />        – miniatúra s lazy loadingom a fallbackom
//   <ProductHoverCard item>…</…>          – hover náhľad nad ľubovoľným riadkom
//   <ProductDetailView sku />             – plný detail (fetch + render), jadro
//   <ProductDetailModal sku onClose />    – detail v modali; rovnaké jadro ako stránka
//
// Stránka /products/:sku aj modal používajú to isté ProductDetailView,
// takže rozšírenia (nové polia, akcie) sa robia na jednom mieste.

import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { X, Upload, ExternalLink, ImageOff } from 'lucide-react';
import { Button } from '../ui/Button.new';
import { getProductDetail, type ProductDetail } from '../../api/stock';
import { pushProductsToShop } from '../../api/upgates';

const SHOPS = ['biketrek', 'xtrek'];

// ── Miniatúra ────────────────────────────────────────────────────────────

export function ProductThumb({ url, name, size = 40 }: { url?: string | null; name?: string; size?: number }) {
  const [failed, setFailed] = useState(false);
  if (!url || failed) {
    return (
      <div
        className="flex items-center justify-center rounded shrink-0"
        style={{ width: size, height: size, backgroundColor: 'var(--color-bg-primary)', color: 'var(--color-text-tertiary)' }}
        title={name}
      >
        <ImageOff size={Math.max(12, size / 3)} />
      </div>
    );
  }
  return (
    <img
      src={url}
      alt={name || ''}
      loading="lazy"
      onError={() => setFailed(true)}
      className="rounded object-contain shrink-0"
      style={{ width: size, height: size, backgroundColor: '#fff' }}
    />
  );
}

// ── Hover náhľad ─────────────────────────────────────────────────────────

export interface HoverItem {
  sku: string;
  name: string;
  brand?: string;
  image_url?: string | null;
  onHand?: number;
  available?: number;
}

export function ProductHoverCard({ item, children }: { item: HoverItem; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <span
      className="relative"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
    >
      {children}
      {open && (
        <span
          className="absolute z-40 left-0 top-full mt-1 flex gap-3 rounded-lg border p-3 shadow-xl"
          style={{
            backgroundColor: 'var(--color-bg-secondary)',
            borderColor: 'var(--color-border-subtle)',
            width: 320,
            pointerEvents: 'none',
          }}
        >
          <ProductThumb url={item.image_url} name={item.name} size={96} />
          <span className="flex flex-col text-left">
            <span className="text-xs" style={{ fontFamily: 'var(--font-mono)', color: 'var(--color-accent)' }}>{item.sku}</span>
            <span className="text-sm mt-0.5" style={{ color: 'var(--color-text-primary)' }}>{item.name}</span>
            {item.brand && <span className="text-xs mt-0.5" style={{ color: 'var(--color-text-secondary)' }}>{item.brand}</span>}
            {item.onHand !== undefined && (
              <span className="text-xs mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
                Na sklade: {item.onHand}{item.available !== undefined ? ` · dostupné: ${item.available}` : ''}
              </span>
            )}
          </span>
        </span>
      )}
    </span>
  );
}

// ── Jadro detailu (zdieľané stránkou aj modalom) ─────────────────────────

export function ProductDetailView({ sku, compact = false }: { sku: string; compact?: boolean }) {
  const [detail, setDetail] = useState<ProductDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pushMsg, setPushMsg] = useState<string | null>(null);
  const [pushing, setPushing] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    getProductDetail(sku)
      .then(setDetail)
      .catch((e) => setError(e?.message || 'Produkt sa nepodarilo načítať'))
      .finally(() => setLoading(false));
  }, [sku]);

  const handlePush = async (shop: string) => {
    setPushing(shop);
    setPushMsg(null);
    try {
      const res = await pushProductsToShop(shop, [sku]);
      setPushMsg(res.skipped.length ? `${res.message} — ${res.skipped[0].reason}` : res.message);
      const d = await getProductDetail(sku);
      setDetail(d);
    } catch (e: any) {
      setPushMsg(e?.message || 'Upload zlyhal');
    } finally {
      setPushing(null);
    }
  };

  if (loading) return <div className="p-6" style={{ color: 'var(--color-text-tertiary)' }}>Načítavam…</div>;
  if (error || !detail) return <div className="p-6" style={{ color: 'var(--color-error)' }}>{error || 'Produkt nenájdený'}</div>;

  const imgSize = compact ? 'w-44 h-44' : 'w-64 h-64';

  return (
    <div className={compact ? 'flex gap-4 items-start' : 'flex gap-6 items-start'}>
      <div
        className={`${imgSize} flex items-center justify-center rounded-xl border shrink-0 overflow-hidden`}
        style={{ borderColor: 'var(--color-border-subtle)', backgroundColor: '#fff' }}
      >
        {detail.image_url ? (
          <img src={detail.image_url} alt={detail.name} className="max-w-full max-h-full object-contain" />
        ) : (
          <span className="text-xs px-4 text-center" style={{ color: 'var(--color-text-tertiary)', backgroundColor: 'var(--color-bg-secondary)', width: '100%', height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            Bez obrázka (produkt nemá uložený obsah z Upgates)
          </span>
        )}
      </div>

      <div className="flex-1 min-w-0">
        <div className="text-xs" style={{ fontFamily: 'var(--font-mono)', color: 'var(--color-accent)' }}>{detail.sku}</div>
        <h2 className="text-lg font-semibold mt-1" style={{ color: 'var(--color-text-primary)' }}>{detail.name}</h2>
        <div className="text-sm mt-1" style={{ color: 'var(--color-text-secondary)' }}>
          {detail.brand || '—'}{detail.group ? ` · skupina ${detail.group.code}` : ''}
          {detail.validation_required && <span style={{ color: 'var(--color-warning, #eab308)' }}> · vyžaduje kontrolu</span>}
        </div>

        <div className="grid grid-cols-3 gap-2 mt-3 max-w-sm">
          <MiniStat label="Na sklade" value={detail.stock.on_hand} />
          <MiniStat label="Rezervované" value={detail.stock.reserved} />
          <MiniStat label="Dostupné" value={detail.stock.available} />
        </div>

        {detail.attributes.length > 0 && (
          <div className="mt-3 text-sm" style={{ color: 'var(--color-text-secondary)' }}>
            {detail.attributes.map((a) => (
              <span key={a.name} className="mr-4">{a.name}: <b style={{ color: 'var(--color-text-primary)' }}>{a.value}</b></span>
            ))}
          </div>
        )}

        <div className="mt-3 text-xs space-y-0.5" style={{ color: 'var(--color-text-tertiary)' }}>
          {detail.identifiers.map((i) => (
            <div key={i.value}>{i.type.toUpperCase()}: <span style={{ fontFamily: 'var(--font-mono)' }}>{i.value}</span>{i.is_primary ? ' (primárny)' : ''}</div>
          ))}
          <div>Vytvorené: {detail.created_at?.slice(0, 16).replace('T', ' ')} · zdroj: {detail.created_from_source || 'neznámy'}</div>
          {detail.shops.map((s) => (
            <div key={s.shop}>V shope <b>{s.shop}</b>: {s.external_code}{s.variant_code ? ` / ${s.variant_code}` : ''} · „{s.shop_availability || '—'}“</div>
          ))}
        </div>

        <div className="mt-4 flex gap-2 items-center flex-wrap">
          {SHOPS.map((shop) => {
            const already = detail.shops.some((s) => s.shop === shop);
            return (
              <Button
                key={shop}
                variant="secondary"
                size="sm"
                disabled={already || pushing !== null}
                title={already ? `Už je v shope ${shop}` : `Nahrať produkt do ${shop} cez Upgates API`}
                onClick={() => handlePush(shop)}
              >
                <Upload size={14} />
                {pushing === shop ? 'Nahrávam…' : already ? `V ${shop} ✓` : `Upload do ${shop}`}
              </Button>
            );
          })}
        </div>
        {pushMsg && <div className="mt-2 text-sm" style={{ color: 'var(--color-text-secondary)' }}>{pushMsg}</div>}
      </div>
    </div>
  );
}

function MiniStat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg border px-2.5 py-1.5" style={{ borderColor: 'var(--color-border-subtle)' }}>
      <div className="text-base font-semibold" style={{ color: 'var(--color-text-primary)' }}>{value}</div>
      <div className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>{label}</div>
    </div>
  );
}

// ── Modal (znovupoužiteľný — sklad, naskladňovanie, …) ───────────────────

export function ProductDetailModal({ sku, onClose }: { sku: string; onClose: () => void }) {
  const navigate = useNavigate();
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-6"
      style={{ backgroundColor: 'rgba(0,0,0,0.6)' }}
      onClick={onClose}
    >
      <div
        className="rounded-xl border w-full max-w-2xl max-h-[85vh] overflow-auto"
        style={{ backgroundColor: 'var(--color-bg-secondary)', borderColor: 'var(--color-border-subtle)' }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-5 py-3 border-b" style={{ borderColor: 'var(--color-border-subtle)' }}>
          <button
            className="text-xs flex items-center gap-1 underline"
            style={{ color: 'var(--color-text-tertiary)' }}
            onClick={() => { onClose(); navigate(`/products/${encodeURIComponent(sku)}`); }}
          >
            <ExternalLink size={12} /> Otvoriť celý detail
          </button>
          <button onClick={onClose} style={{ color: 'var(--color-text-tertiary)' }}><X size={18} /></button>
        </div>
        <div className="p-5">
          <ProductDetailView sku={sku} compact />
        </div>
      </div>
    </div>
  );
}
