import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { ProductImportDraft, ProductImportRow } from '../../api/productImport';
import { emptySkuIdentity, SkuIdentityInput, SkuPreview } from '../../api/skuPolicy';
import { SkuIdentityForm } from './SkuIdentityForm';
import { Button } from '../ui/Button.new';

export function ImportSkuIdentityDialog({ row, draft, saving, savesOtherEdits, error, onApply, onClose, onDirtyChange }: {
  row: ProductImportRow; draft: ProductImportDraft; saving: boolean; savesOtherEdits: boolean; error: string;
  onApply: (value: SkuIdentityInput | null) => void;
  onClose: () => void; onDirtyChange: (dirty: boolean) => void;
}) {
  const { t } = useTranslation();
  const suppliedIdentity = row.values.sku_identity;
  const manufacturerChanged = suppliedIdentity?.code_kind === 'manufacturer' && row.values.item_type !== 'service'
    && !row.hub_product_id && !row.expected_hub_product_id
    && (suppliedIdentity.brand !== row.values.brand || suppliedIdentity.source_code !== row.values.manufacturer_code);
  const initial = useRef(suppliedIdentity && !manufacturerChanged && (suppliedIdentity.code_kind === 'manufacturer' || row.values.item_type === 'service')
    ? suppliedIdentity : { ...emptySkuIdentity(row.values.brand, row.values.manufacturer_code), supplier_id: draft.supplier_id });
  const [value, setValue] = useState(initial.current), [preview, setPreview] = useState<SkuPreview | null>(null);
  const panel = useRef<HTMLDivElement>(null);
  const dirty = JSON.stringify(value) !== JSON.stringify(initial.current);
  const close = () => { if (!saving && (!dirty || window.confirm(t('productImport.discardCell')))) onClose(); };
  const closeRef = useRef(close); closeRef.current = close;
  useEffect(() => { onDirtyChange(dirty); return () => onDirtyChange(false); }, [dirty, onDirtyChange]);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null, overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const firstField = !initial.current.brand ? 'sku-brand' : !initial.current.source_code ? 'sku-source-code' : 'sku-leaf-confirmed';
    panel.current?.querySelector<HTMLElement>(`[data-testid="${firstField}"]`)?.focus();
    const listener = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); closeRef.current(); }
      if (event.key === 'Tab') {
        const nodes = Array.from(panel.current?.querySelectorAll<HTMLElement>('input:not(:disabled),select:not(:disabled),button:not(:disabled),summary') || [])
          .filter(node => !node.closest('details:not([open])') || node.tagName === 'SUMMARY');
        const first = nodes[0], last = nodes[nodes.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    };
    document.addEventListener('keydown', listener);
    return () => { document.body.style.overflow = overflow; document.removeEventListener('keydown', listener); previous?.focus(); };
  }, []);
  const canSave = preview?.status === 'new' || preview?.status === 'existing';
  const proof = preview?.status === 'new' || (value.leaf_confirmed && preview?.brand_id) ? value : null;
  return createPortal(<div className="product-import product-import-editor-backdrop"><section ref={panel} role="dialog" aria-modal="true" aria-label={t('skuPolicy.title')} className="product-import-cell-editor">
    <header><div><h2>{t('skuPolicy.title')}</h2><small>{row.values.name} · {row.is_variant ? t('productImport.variant') : t('productImport.standalone')}</small></div><Button variant="ghost" disabled={saving} onClick={close}>{t('common.close')}</Button></header>
    <div className="import-cell-editor-body"><SkuIdentityForm simplified disabled={saving} value={value} onChange={setValue} onPreview={setPreview}
      source={{ supplierCode: row.values.supplier_code, manufacturerCode: row.values.manufacturer_code, groupCode: row.is_variant ? row.group_key : undefined }}
      context={{ item_type: row.values.item_type || 'stock', barcodes: row.values.eans, supplier_id: draft.supplier_id, supplier_sku: row.values.supplier_code, sku: row.values.code, expected_product_id: row.expected_hub_product_id || undefined }} />
      {savesOtherEdits && <p className="import-muted sku-draft-message">{t('skuPolicy.savesOtherEdits')}</p>}
      {error && <p className="import-error sku-draft-message" role="alert">{error}</p>}
    </div>
    <footer>
      <Button variant="ghost" disabled={saving} onClick={close}>{t('common.cancel')}</Button>
      <Button data-testid="import-sku-apply" disabled={saving || !canSave} onClick={() => onApply(proof)}>
        {t(saving ? 'skuPolicy.savingDraft' : 'skuPolicy.saveDraft')}
      </Button>
    </footer>
  </section></div>, document.body);
}
