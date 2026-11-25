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
    listSupplierPreorders({ product_id: productId }, controller.signal).then(result => {
      if (!controller.signal.aborted) setItems(result.items.filter(order => order.status === 'open'));
    }).catch(() => { /* Continue to show the product card when planning data is unavailable. */ });
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
