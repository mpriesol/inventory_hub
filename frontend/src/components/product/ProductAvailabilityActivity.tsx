import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { getProductAvailabilityActivity, type ProductAvailabilityActivityData } from '../../api/productEditor';

export function ProductAvailabilityActivity({ productId, refresh }: { productId: number; refresh: number }) {
  const { t, i18n } = useTranslation();
  const [activity, setActivity] = useState<ProductAvailabilityActivityData | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setError(false);
    getProductAvailabilityActivity(productId, controller.signal).then(value => {
      if (!controller.signal.aborted) setActivity(value);
    }, () => {
      if (!controller.signal.aborted) { setActivity(null); setError(true); }
    });
    return () => controller.abort();
  }, [productId, refresh]);

  const date = (stamp: string) => new Date(stamp).toLocaleString(i18n.language);
  const state = (kind: string, value: string | null) =>
    value == null ? '—' : kind === 'supplier' ? t(`stockCard.availabilityState.${value}`, { defaultValue: value }) : value;
  return <section className="stock-card__block" data-testid="stock-card-availability-activity">
    <h4>{t('stockCard.lastShopSync')}</h4>
    {error && <p role="status">{t('stockCard.availabilityActivityFailed')}</p>}
    {!error && !activity && <p>{t('stockCard.loading')}</p>}
    {activity && <>
      {activity.shops.length ? <dl className="stock-card__details stock-card__details--compact">
        {activity.shops.map(shop => <div key={shop.shop_code}>
          <dt>{shop.shop_name}</dt>
          <dd>{shop.last_sync_at ? date(shop.last_sync_at) : t('stockCard.noShopSync')}</dd>
        </div>)}
      </dl> : <p>{t('stockCard.noShopSync')}</p>}
      <details>
        <summary>{t('stockCard.availabilityChanges')}</summary>
        {activity.changes.length ? <ul className="stock-card__supplier-list">
          {activity.changes.map(change => <li key={`${change.kind}:${change.event_id}`}>
            <strong>{change.source}</strong>
            <span>{state(change.kind, change.before)} → {state(change.kind, change.after)}</span>
            <time dateTime={change.changed_at}>{date(change.changed_at)}</time>
          </li>)}
        </ul> : <p>{t('stockCard.noAvailabilityChanges')}</p>}
      </details>
    </>}
  </section>;
}
