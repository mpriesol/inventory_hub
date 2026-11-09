import React from 'react';
import { useTranslation } from 'react-i18next';
import { stockCardSafeUrl, type StockCardDraft } from './stockCardHelpers';

export function StockCardEditor({ draft, disabled, onChange }: {
  draft: StockCardDraft; disabled: boolean; onChange: (key: string, value: string) => void;
}) {
  const { t } = useTranslation();
  const c = (key: string) => t(`stockCard.${key}`);
  const { values, base } = draft;
  const input = (key: string, extra: React.InputHTMLAttributes<HTMLInputElement> = {}) => <input
    data-testid={`stock-card-input-${key}`} value={values[key] || ''} onChange={event => onChange(key, event.target.value)} {...extra} />;
  const supplier = base.suppliers?.find(row => row.is_primary && row.is_active) || base.suppliers?.find(row => row.is_active) || base.suppliers?.[0];
  return <fieldset disabled={disabled} className="stock-card__editor" data-testid="stock-card-editor">
    {draft.kind === 'product' && <>
      <label className="stock-card__field stock-card__field--full">{c('name')}{input('name', { maxLength: 500, required: true })}</label>
      <div className="stock-card__form-grid">
        <div className="stock-card__field"><span>{c('supplier')}</span><p className="stock-card__fixed-value" data-testid="stock-card-editor-supplier">{supplier?.supplier_name || c('notLinked')}{supplier && !supplier.is_active && <span className="stock-card__badge stock-card__supplier-status">{c('inactiveSupplierSource')}</span>}</p><small>{c('supplierChangeHelp')}</small></div>
        <label className="stock-card__field">{c('brand')}{input('brand', { maxLength: 100 })}</label>
        <label className="stock-card__field">{c('ean')}<textarea data-testid="stock-card-input-eans" value={values.eans} rows={2} onChange={event => onChange('eans', event.target.value)} /><small>{c('eanHelp')}</small></label>
        <div className="stock-card__field"><span>{c('supplierCode')}</span><p className="stock-card__fixed-value stock-card__mono">{supplier?.supplier_sku || base.supplier_codes.map(row => row.code).join(', ') || c('notSet')}</p></div>
        <label className="stock-card__field">{c('location')}{input('location', { maxLength: 100, disabled: !base.warehouse_context })}</label>
        <div className="stock-card__field"><span>{c('typeUnit')}</span><div className="stock-card__inline-fields">
          <select aria-label={c('itemType')} data-testid="stock-card-input-item_type" disabled={base.item_definition_locked} value={values.item_type} onChange={event => onChange('item_type', event.target.value)}>
            <option value="stock">{c('goods')}</option><option value="service">{c('service')}</option>
          </select>
          <select aria-label={c('unit')} data-testid="stock-card-input-stock_unit" disabled={base.item_definition_locked || values.item_type === 'service'} value={values.stock_unit} onChange={event => onChange('stock_unit', event.target.value)}>
            {['pcs', 'pair', 'pack', 'm'].map(value => <option key={value} value={value}>{t(`productEditor.units.${value}`)}</option>)}
          </select>
        </div>{base.item_definition_locked && <small>{c('definitionLocked')}</small>}{values.stock_unit === 'm' && <small>{t('productEditor.meterLimit')}</small>}</div>
      </div>
      <label className="stock-card__field">{c('description')}<textarea data-testid="stock-card-input-description" rows={6} maxLength={20000} disabled={values.restore_description === 'true'} value={values.description} onChange={event => onChange('description', event.target.value)} /></label>
      {Object.prototype.hasOwnProperty.call(base.overrides.common || {}, 'description') && <label className="stock-card__check"><input type="checkbox" data-testid="stock-card-restore-description" checked={values.restore_description === 'true'} onChange={event => onChange('restore_description', String(event.target.checked))} />{c('restoreDescription')}</label>}
      <label className="stock-card__field">{c('imageUrl')}{input('image_url', { type: 'url', maxLength: 2000, placeholder: 'https://' })}
        {values.image_url && (!stockCardSafeUrl(values.image_url) || !values.image_url.startsWith('https://')) && <small className="stock-card__error">{c('invalidImageUrl')}</small>}</label>
      <div className="stock-card__form-grid">
        <label className="stock-card__field">{c('minimumStock')}{input('min_quantity', { inputMode: 'numeric', disabled: !base.warehouse_context || values.item_type !== 'stock' })}</label>
        <label className="stock-card__field">{c('activity')}<select value={values.is_active} onChange={event => onChange('is_active', event.target.value)}>
          <option value="true">{c('active')}</option><option value="false">{c('inactive')}</option>
        </select></label>
      </div>
      <label className="stock-card__field">{c('internalNote')}<textarea data-testid="stock-card-input-internal_note" rows={3} value={values.internal_note} onChange={event => onChange('internal_note', event.target.value)} /></label>
    </>}
    {draft.kind === 'prices' && <>
      <div className="stock-card__form-grid">
        <label className="stock-card__field">{c('sellingPriceGross')}{input('sale_price_gross', { inputMode: 'decimal' })}<small>{c('priceCurrency')}</small></label>
        <label className="stock-card__field">{c('vatRate')}{input('vat_rate', { inputMode: 'decimal' })}</label>
      </div>
      <p className="stock-card__note">{c('priceSaveHelp')}</p>
      <h4>{c('shopPrices')}</h4>
      {base.shops.filter(shop => ['biketrek', 'xtrek'].includes(shop.shop_code)).map(shop => <div className="stock-card__shop-price-edit" key={shop.shop_code}>
        <strong>{shop.shop_code === 'biketrek' ? 'BIKETREK' : 'xTrek'}</strong>
        <label className="stock-card__check"><input type="checkbox" checked={values[`${shop.shop_code}_inherit`] === 'true'} onChange={event => onChange(`${shop.shop_code}_inherit`, String(event.target.checked))} />{c('inheritPrice')}</label>
        {values[`${shop.shop_code}_inherit`] !== 'true' && <label className="stock-card__field">{c('sellingPriceGross')}{input(`${shop.shop_code}_price`, { inputMode: 'decimal' })}</label>}
      </div>)}
    </>}
    {draft.kind === 'links' && <>
      <p className="stock-card__note">{c('navigationHelp')}</p>
      <label className="stock-card__field">{c('shopProductLink')}{input('shop_url', { type: 'url', maxLength: 2000, disabled: values.restore_links === 'true', placeholder: 'https://' })}</label>
      <label className="stock-card__field">{c('shopAdminLink')}{input('shop_admin_url', { type: 'url', maxLength: 2000, disabled: values.restore_links === 'true', placeholder: 'https://' })}</label>
      <label className="stock-card__check"><input type="checkbox" data-testid="stock-card-restore-links" checked={values.restore_links === 'true'} onChange={event => onChange('restore_links', String(event.target.checked))} />{c('restoreLinks')}</label>
    </>}
  </fieldset>;
}
