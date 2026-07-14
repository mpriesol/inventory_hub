// src/pages/ProductDetailPage.tsx
import React, { useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { ArrowLeft, Upload } from 'lucide-react';
import { Button } from '../components/ui/Button.new';
import { getProductDetail, type ProductDetail } from '../api/stock';
import { pushProductsToShop } from '../api/upgates';

const SHOPS = ['biketrek', 'xtrek'];

export function ProductDetailPage() {
  const { sku } = useParams<{ sku: string }>();
  const navigate = useNavigate();
  const [detail, setDetail] = useState<ProductDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pushMsg, setPushMsg] = useState<string | null>(null);
  const [pushing, setPushing] = useState<string | null>(null);

  useEffect(() => {
    if (!sku) return;
    setLoading(true);
    getProductDetail(sku)
      .then(setDetail)
      .catch((e) => setError(e?.message || 'Produkt sa nepodarilo načítať'))
      .finally(() => setLoading(false));
  }, [sku]);

  const handlePush = async (shop: string) => {
    if (!sku) return;
    setPushing(shop);
    setPushMsg(null);
    try {
      const res = await pushProductsToShop(shop, [sku]);
      setPushMsg(res.skipped.length ? `${res.message} — ${res.skipped[0].reason}` : res.message);
    } catch (e: any) {
      setPushMsg(e?.message || 'Upload zlyhal');
    } finally {
      setPushing(null);
    }
  };

  if (loading) return <div className="p-8" style={{ color: 'var(--color-text-tertiary)' }}>Načítavam…</div>;
  if (error || !detail) return (
    <div className="p-8">
      <Button variant="secondary" size="sm" onClick={() => navigate('/stock')}><ArrowLeft size={14} /> Späť na sklad</Button>
      <div className="mt-4" style={{ color: 'var(--color-error)' }}>{error || 'Produkt nenájdený'}</div>
    </div>
  );

  return (
    <div className="p-6 max-w-5xl">
      <Button variant="secondary" size="sm" onClick={() => navigate('/stock')}>
        <ArrowLeft size={14} /> Späť na sklad
      </Button>

      <div className="mt-4 flex gap-6 items-start">
        {/* Obrázok priamo z Upgates CDN — žiadne lokálne úložisko */}
        <div
          className="w-64 h-64 flex items-center justify-center rounded-xl border shrink-0 overflow-hidden"
          style={{ borderColor: 'var(--color-border-subtle)', backgroundColor: 'var(--color-bg-secondary)' }}
        >
          {detail.image_url ? (
            <img src={detail.image_url} alt={detail.name} className="max-w-full max-h-full object-contain" />
          ) : (
            <span className="text-xs px-4 text-center" style={{ color: 'var(--color-text-tertiary)' }}>
              Bez obrázka (produkt nemá uložený obsah z Upgates)
            </span>
          )}
        </div>

        <div className="flex-1">
          <div className="text-xs" style={{ fontFamily: 'var(--font-mono)', color: 'var(--color-accent)' }}>{detail.sku}</div>
          <h1 className="text-xl font-semibold mt-1" style={{ color: 'var(--color-text-primary)' }}>{detail.name}</h1>
          <div className="text-sm mt-1" style={{ color: 'var(--color-text-secondary)' }}>
            {detail.brand || '—'}{detail.group ? ` · skupina ${detail.group.code}` : ''}
          </div>

          <div className="grid grid-cols-3 gap-3 mt-4 max-w-md">
            <Stat label="Na sklade" value={detail.stock.on_hand} />
            <Stat label="Rezervované" value={detail.stock.reserved} />
            <Stat label="Dostupné" value={detail.stock.available} />
          </div>

          {detail.attributes.length > 0 && (
            <div className="mt-4 text-sm" style={{ color: 'var(--color-text-secondary)' }}>
              {detail.attributes.map((a) => (
                <span key={a.name} className="mr-4">{a.name}: <b style={{ color: 'var(--color-text-primary)' }}>{a.value}</b></span>
              ))}
            </div>
          )}

          <div className="mt-4 text-xs space-y-1" style={{ color: 'var(--color-text-tertiary)' }}>
            {detail.identifiers.map((i) => (
              <div key={i.value}>{i.type.toUpperCase()}: <span style={{ fontFamily: 'var(--font-mono)' }}>{i.value}</span>{i.is_primary ? ' (primárny)' : ''}</div>
            ))}
            <div>Vytvorené: {detail.created_at?.slice(0, 16).replace('T', ' ')} · zdroj: {detail.created_from_source || 'neznámy'}</div>
            {detail.shops.map((s) => (
              <div key={s.shop}>V shope <b>{s.shop}</b>: kód {s.external_code}{s.variant_code ? ` / ${s.variant_code}` : ''}, dostupnosť „{s.shop_availability || '—'}“</div>
            ))}
          </div>

          <div className="mt-5 flex gap-2 items-center">
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
                  {pushing === shop ? 'Nahrávam…' : already ? `V shope ${shop} ✓` : `Upload do ${shop}`}
                </Button>
              );
            })}
          </div>
          {pushMsg && <div className="mt-2 text-sm" style={{ color: 'var(--color-text-secondary)' }}>{pushMsg}</div>}
        </div>
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg border px-3 py-2" style={{ borderColor: 'var(--color-border-subtle)' }}>
      <div className="text-lg font-semibold" style={{ color: 'var(--color-text-primary)' }}>{value}</div>
      <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>{label}</div>
    </div>
  );
}

export default ProductDetailPage;
