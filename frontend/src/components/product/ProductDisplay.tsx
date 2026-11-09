// Product thumbnails and passive hover previews. Full cards use StockCard.
import React, { useState } from 'react';
import { ImageOff } from 'lucide-react';

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
