import React, { useRef, useState } from 'react';
import { StockCard } from './StockCard';
import type { ProductEditorDetail } from '../../api/productEditor';

type Props = Omit<React.AnchorHTMLAttributes<HTMLAnchorElement>, 'href'> & {
  productId?: number | null;
  sku?: string | null;
  warehouseCode?: string | null;
  onChanged?: (row: ProductEditorDetail) => void;
};

/** A normal link for new tabs; an in-place card for an ordinary click. */
export function StockCardLink({ productId, sku, warehouseCode, onChanged, onClick, children, ...anchor }: Props) {
  const [open, setOpen] = useState(false);
  const changed = useRef<ProductEditorDetail | null>(null);
  const id = productId && Number.isSafeInteger(productId) && productId > 0 ? productId : undefined;
  const path = id ? `/products/card/${id}` : `/products/${encodeURIComponent(sku || '')}`;
  const query = warehouseCode !== undefined ? `?${new URLSearchParams({ warehouse: warehouseCode || '' })}` : '';
  return <>
    <a {...anchor} href={`${path}${query}`} onClick={event => {
      onClick?.(event);
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || anchor.target === '_blank') return;
      event.preventDefault();
      event.stopPropagation();
      changed.current = null;
      setOpen(true);
    }}>{children}</a>
    {open && <StockCard productId={id} sku={sku || undefined} warehouseCode={warehouseCode} onClose={() => {
      setOpen(false);
      if (changed.current) { const row = changed.current; changed.current = null; onChanged?.(row); }
    }} onChanged={row => { changed.current = row; }} />}
  </>;
}
