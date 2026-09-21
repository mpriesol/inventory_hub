import React from 'react';
import { useTranslation } from 'react-i18next';
import { AiPolicy, policyKeys } from '../../api/aiContent';

export function AiPolicyFields({ value, onChange, origins }: { value: AiPolicy; onChange?: (value: AiPolicy) => void; origins?: Record<string, string> }) {
  const { t } = useTranslation();
  return <div className="ai-policy">{policyKeys.map(key => <label key={key}>
    <span>{t(`ai.policy.${key}`)}</span>
    {onChange ? <select value={value[key] == null ? 'inherit' : String(value[key])}
      onChange={e => onChange({ ...value, [key]: e.target.value === 'inherit' ? null : e.target.value === 'true' })}>
      <option value="inherit">{t('ai.inherit')}</option><option value="true">{t('ai.on')}</option><option value="false">{t('ai.off')}</option>
    </select> : <strong>{t(value[key] ? 'ai.on' : 'ai.off')}</strong>}
    {origins?.[key] && <small>{t('ai.fromRule')}: {origins[key] === 'run' ? t('ai.thisRun') : origins[key]}</small>}
  </label>)}</div>;
}
