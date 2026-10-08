import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AiSettings, readAiSettings, saveAiSettings } from '../../api/aiContent';

export function AiModelSettings({ onChange }: { onChange: (settings: AiSettings) => void }) {
  const { t, i18n } = useTranslation();
  const [settings, setSettings] = useState<AiSettings | null>(null);
  const [model, setModel] = useState('');
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState(false);
  const [saved, setSaved] = useState(false);
  const selected = settings?.models.find(item => item.id === model);
  const amount = (value: string) => new Intl.NumberFormat(i18n.language, { maximumFractionDigits: 4 }).format(Number(value));
  const describeError = (error: Error & { code?: string }) => t(`ai.errors.${error.code}`, { defaultValue: error.message });
  function accept(value: AiSettings) { setSettings(value); setModel(value.model); setConflict(false); onChange(value); }
  useEffect(() => {
    let stopped = false;
    readAiSettings().then(value => { if (!stopped) accept(value); }).catch(error => { if (!stopped) setError(describeError(error)); }).finally(() => { if (!stopped) setBusy(false); });
    return () => { stopped = true; };
  }, []);
  async function reload() {
    setBusy(true); setError(''); setSaved(false);
    try { accept(await readAiSettings()); } catch (error) { setError(describeError(error as Error)); } finally { setBusy(false); }
  }
  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!settings || !selected || busy || conflict) return;
    setBusy(true); setError(''); setSaved(false);
    try { const value = await saveAiSettings(settings, model); accept(value); setSaved(true); }
    catch (error) { setError(describeError(error as Error)); setConflict((error as { code?: string }).code === 'ai_settings_changed'); }
    finally { setBusy(false); }
  }
  return <form className="ai-card ai-model-settings" onSubmit={save} aria-labelledby="ai-model-settings-title">
    <h2 id="ai-model-settings-title">{t('ai.modelSettings.title')}</h2>
    <p>{t('ai.modelSettings.help')}</p>
    {busy && !settings && <p role="status">{t('common.loading')}</p>}
    {settings && <>
      <div className="ai-grid"><label>{t('ai.modelSettings.model')}<select value={model} disabled={busy || conflict} onChange={event => { setModel(event.target.value); setSaved(false); }}>
        {!settings.models.some(item => item.id === model) && <option value={model}>{model}</option>}
        {settings.models.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
      </select></label><div className="ai-model-current"><small>{t('ai.modelSettings.current')}</small><strong>{settings.models.find(item => item.id === settings.model)?.label || settings.model}</strong><small>{t(`ai.modelSettings.source.${settings.source}`)}</small></div></div>
      {selected && <div className="ai-model-pricing">
        <p>{t('ai.modelSettings.price', { input: amount(selected.input_usd_per_million), output: amount(selected.output_usd_per_million) })}</p>
        <small>{t('ai.modelSettings.priceHelp')}</small>
        <details><summary>{t('ai.modelSettings.priceDetails')}</summary>
          <p>{t('ai.modelSettings.cachePrice', { cached: amount(selected.cached_input_usd_per_million), write: amount(selected.cache_write_usd_per_million) })}</p>
          <p>{t('ai.modelSettings.searchPrice', { amount: amount(selected.web_search_usd) })}</p>
          <p>{t('ai.modelSettings.longPrice', { threshold: selected.long_context_threshold.toLocaleString(i18n.language), input: amount(selected.long_context_input_usd_per_million), output: amount(selected.long_context_output_usd_per_million) })}</p>
        </details>
      </div>}
      <p className="ai-notice">{t('ai.modelSettings.newJobs')}</p>
      <div className="ai-actions"><button className="ai-primary" disabled={busy || conflict || !selected || model === settings.model}>{t('ai.modelSettings.save')}</button></div>
    </>}
    {error && <div className="ai-notice ai-error" role="alert">{error}<div className="ai-actions"><button type="button" disabled={busy} onClick={reload}>{t('ai.modelSettings.reload')}</button></div></div>}
    {saved && <p role="status">{t('ai.modelSettings.saved')}</p>}
  </form>;
}
