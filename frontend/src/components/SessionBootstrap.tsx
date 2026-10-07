import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { restoreSession } from '../api/access';

export function SessionBootstrap({children}: {children: React.ReactNode}) {
  const { t } = useTranslation();
  const [ready, setReady] = useState(false);
  useEffect(() => {
    let active = true;
    void restoreSession().catch(() => {}).finally(() => { if (active) setReady(true); });
    const refresh = () => { void restoreSession().catch(() => {}); };
    window.addEventListener('focus', refresh);
    return () => { active = false; window.removeEventListener('focus', refresh); };
  }, []);
  return ready ? <>{children}</> : <p role="status">{t('common.loading')}</p>;
}
