import React from 'react';
import { useTranslation } from 'react-i18next';
import './ActionScope.css';

export type ActionEffect = 'hub-read' | 'hub-write' | 'upgates-read' | 'upgates-write' | 'queued-upgates';
export type ActionCalls = { kind: 'known'; count: number } | { kind: 'unknown' } | { kind: 'variable' };
export interface ActionScopeProps {
  effects: ActionEffect[];
  shop?: string;
  /** A documented plan, never a count of browser requests. */
  calls?: ActionCalls;
  /** Only supply measurements returned by the backend for this operation. */
  actualCalls?: number;
  children?: React.ReactNode;
  className?: string;
}

/** Explicit source and effects, including work scheduled after the HTTP response. */
export function ActionScope({ effects, shop, calls, actualCalls, children, className = '' }: ActionScopeProps) {
  const { t } = useTranslation();
  const unique = [...new Set(effects)];
  const external = unique.some(effect => effect.includes('upgates'));
  const target = shop === 'biketrek' ? 'BIKETREK' : shop === 'xtrek' ? 'xTrek' : shop;
  const count = calls?.kind === 'known' && Number.isInteger(calls.count) && calls.count >= 0 ? calls.count : null;
  const measured = actualCalls !== undefined && Number.isInteger(actualCalls) && actualCalls >= 0;
  return <span className={`action-scope ${className}`} data-action-effects={unique.join(' ')}>
    <span className="action-scope-effects">{unique.map(effect => <span className={`action-scope-effect action-scope-${effect}`} key={effect}>{t(`actions.effects.${effect}`)}</span>)}{external && target && <span>{target}</span>}</span>
    {external && <span className="action-scope-calls">{count !== null ? t('actions.callsKnown', { count }) : t(calls?.kind === 'variable' ? 'actions.callsVariable' : 'actions.callsUnknown')}</span>}
    {external && <span className="action-scope-measured">{measured ? t('actions.callsActual', { count: actualCalls }) : t('actions.callsUnmeasured')}</span>}
    {children && <span className="action-scope-detail">{children}</span>}
  </span>;
}
