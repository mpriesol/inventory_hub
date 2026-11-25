import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { listSupplierPreorders, SupplierPreorder } from '../../api/supplierPreorders';
import { invoiceDate } from '../../pages/invoiceArchiveUi';
import { formatNumber } from '../../utils/format';

export function SupplierPreorderInfo({ productId }: { productId: number }) {
  const { t, i18n } = useTranslation();
  const [items, setItems] = useState<SupplierPreorder[]>([]);
  useEffect(() => {
    const controller = new AbortController();
    setItems([]);
    async function loadOpenPreorders() {
      const open: SupplierPreorder[] = [];
      let checked = 0;
      for (let page = 1; !controller.signal.aborted; page++) {
        const result = await listSupplierPreorders({ product_id: productId, page }, controller.signal);
        open.push(...result.items.filter(order => order.status === 'open'));
        checked += result.items.length;
        if (!result.items.length || checked >= result.total) break;
      }
      if (!controller.signal.aborted) setItems(open);
    }
    void loadOpenPreorders().catch(() => { /* The product card can load independently of planning data. */ });
    return () => controller.abort();
  }, [productId]);
  if (!items.length) return null;
  return <section className="stock-card__block">
    <h4>{t('supplierPreorders.title')}</h4>
    <ul>{items.flatMap(order => order.lines.map(line => <li key={line.id}>
      <Link to={`/supplier-preorders/${order.id}`}>{order.reference}</Link> · {order.supplier_name} · {t('supplierPreorders.productPending', {
        quantity: formatNumber(line.remaining_quantity, i18n.language, 3),
        unit: t(`productEditor.units.${line.stock_unit}`),
        date: invoiceDate(line.expected_on, i18n.language),
      })}
    </li>))}</ul>
  </section>;
}
