import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AiReferenceProduct, aiRequest } from '../../api/aiContent';
import { DescriptionPreview } from './AiJobDetail';

export function AiReferenceProducts({ value, onChange }: {
  value: AiReferenceProduct[]; onChange: (value: AiReferenceProduct[]) => void;
}) {
  const { t } = useTranslation();
  const [shop, setShop] = useState('biketrek');
  const [code, setCode] = useState('');
  const [preview, setPreview] = useState<AiReferenceProduct | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  async function load(targetShop = shop, targetCode = code.trim()) {
    setBusy(true); setError(''); setPreview(null);
    try {
      const result = await aiRequest<{ reference: AiReferenceProduct }>('/reference-products/preview', { shop: targetShop, code: targetCode });
      if (mounted.current) {
        const previous = value.find(item => item.shop === targetShop && item.code === targetCode);
        setPreview({ ...result.reference, guidance: previous?.guidance || '' });
      }
    } catch (error) {
      if (mounted.current) setError(t(`ai.errors.${(error as { code?: string }).code}`, { defaultValue: (error as Error).message }));
    } finally { if (mounted.current) setBusy(false); }
  }
  const remaining = value.filter(item => item.shop !== preview?.shop || item.code !== preview?.code);
  return <section className="ai-card">
    <h3>{t('ai.references.title')}</h3><p>{t('ai.references.help')}</p>
    {value.map(item => <div className="ai-card" key={`${item.shop}/${item.code}`}>
      <strong>{item.content.title}</strong><p>{item.shop} · {item.code} · {new Date(item.captured_at).toLocaleString()}</p>
      <label>{t('ai.references.guidance')}<textarea maxLength={2000} value={item.guidance} onChange={event => onChange(value.map(entry => entry === item ? { ...entry, guidance: event.target.value } : entry))} /></label>
      <div className="ai-actions"><button disabled={busy} onClick={() => load(item.shop, item.code)}>{t('ai.references.refresh')}</button>
        <button disabled={busy} onClick={() => onChange(value.filter(entry => entry !== item))}>{t('ai.references.remove')}</button></div>
      <details><summary>{t('ai.references.savedPreview')}</summary><DescriptionPreview title={item.content.title} value={item.content.long_description} /></details>
    </div>)}
    <div className="ai-grid"><label>{t('ai.references.shop')}<select disabled={busy} value={shop} onChange={e => setShop(e.target.value)}><option value="biketrek">BIKETREK</option><option value="xtrek">xTrek</option></select></label>
      <label>{t('ai.references.code')}<input disabled={busy} value={code} onChange={e => setCode(e.target.value)} placeholder="PL-10462060" /></label></div>
    <button disabled={busy || !code.trim()} onClick={() => load()}>{t(busy ? 'common.loading' : 'ai.references.load')}</button>
    {preview && <div className="ai-card"><h4>{preview.content.title}</h4><p>{preview.shop} · {preview.code}</p><p>{preview.content.short_description}</p>
      <DescriptionPreview title={t('ai.references.preview')} value={preview.content.long_description} />
      <p><strong>SEO:</strong> {preview.content.seo_title}</p><p>{preview.content.meta_description}</p>
      <details><summary>{t('ai.parameterRegistry')} ({preview.content.parameters.length})</summary><dl>{preview.content.parameters.map((p, i) => <React.Fragment key={i}><dt>{p.name}</dt><dd>{p.value}</dd></React.Fragment>)}</dl></details>
      <label>{t('ai.references.guidance')}<textarea maxLength={2000} value={preview.guidance} onChange={e => setPreview({ ...preview, guidance: e.target.value })} placeholder={t('ai.references.guidancePlaceholder')} /></label>
      <p>{t('ai.references.publishHelp')}</p><div className="ai-actions"><button className="ai-primary" disabled={remaining.length >= 2} onClick={() => { onChange([...remaining, preview]); setPreview(null); }}>{t('ai.references.accept')}</button>
        <button onClick={() => setPreview(null)}>{t('ai.references.cancel')}</button></div>
      {remaining.length >= 2 && <p>{t('ai.references.limit')}</p>}
    </div>}
    {error && <p role="alert" className="ai-error">{error}</p>}
  </section>;
}
