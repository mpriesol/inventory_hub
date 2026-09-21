import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AiContent, AiJob, aiRequest } from '../../api/aiContent';
import { AiPolicyFields } from './AiPolicyFields';
import { CatalogImport } from './CatalogImport';

export function AiJobDetail({ job, onChange }: { job: AiJob; onChange: (job: AiJob) => void }) {
  const { t } = useTranslation();
  const [content, setContent] = useState<AiContent | undefined>(job.kind === 'product' ? job.output : undefined);
  const [parameters, setParameters] = useState(JSON.stringify(job.output?.parameters || [], null, 2));
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [importOpen, setImportOpen] = useState(false);
  const editable = ['review', 'blocked', 'ready'].includes(job.status) && job.kind === 'product';
  async function perform(fn: () => Promise<AiJob>) { setBusy(true); setError(''); try { const updated = await fn(); onChange(updated); setDirty(false); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } }
  const action = (action: string) => perform(() => aiRequest<AiJob>(`/jobs/${job.id}/action`, { action, expected_revision: job.revision }));
  const save = (approve: boolean) => perform(() => aiRequest<AiJob>(`/jobs/${job.id}/review`, { expected_revision: job.revision, approve, content: { ...content, parameters: JSON.parse(parameters) } }));
  return <section className="ai-card">
    <div className="ai-row"><h2 className="ai-grow">{job.name} · {job.shop}</h2><span className="ai-badge">{t(`ai.states.${job.status}`, { defaultValue: job.status })}</span></div>
    <p>{job.code} · {t('ai.publishedVersion', { version: job.rules_version })} · {job.category_profile}</p>
    <AiPolicyFields value={job.policy} origins={job.origins} />
    {job.policy.show_cost_estimate !== false && <p>{t('ai.estimate')}: {Number(job.estimate_usd).toFixed(3)} USD · {t('ai.actualCost')}: {job.actual_usd === null ? '—' : `${Number(job.actual_usd).toFixed(4)} USD`}</p>}
    {job.status === 'estimate' && <div className="ai-actions"><button disabled={busy} className="ai-primary" onClick={() => action('start')}>{t('ai.startProcessing')}</button><small>{t('ai.estimateHelp')}</small></div>}
    {job.error && <div className="ai-notice ai-error">{t(`ai.errors.${job.error}`, { defaultValue: job.error })}</div>}
    {job.checks?.errors?.map(v => <div key={v} className="ai-notice ai-error">{t(`ai.errors.${v.split(':')[0]}`, { defaultValue: v })}{v.includes(':') ? ` · ${v.split(':').slice(1).join(':')}` : ''}</div>)}
    {job.checks?.warnings?.map(v => <div key={v} className="ai-notice">{t(`ai.errors.${v}`, { defaultValue: v })}</div>)}
    {job.kind === 'rules' && job.output?.instructions && <><p>{job.output.reason}</p><pre className="ai-original">{job.output.instructions}</pre>{job.output.parameters && <details open><summary>{t('ai.parameterRegistry')}</summary><div className="ai-scroll"><table><thead><tr><th>{t('ai.parameterName')}</th><th>{t('ai.required')}</th><th>{t('ai.parameterScope')}</th><th>{t('ai.allowedValues')}</th></tr></thead><tbody>{job.output.parameters.map(p => <tr key={p.name}><td>{p.name}</td><td>{t(p.required ? 'ai.on' : 'ai.off')}</td><td>{t(`ai.${p.scope}`)}</td><td>{p.values.join(' | ')}</td></tr>)}</tbody></table></div></details>}{job.output.questions?.map(q => <p key={q}>{q}</p>)}{job.status === 'review' && <button disabled={busy} onClick={() => perform(async () => { await aiRequest(`/jobs/${job.id}/accept-proposal`, { expected_revision: job.revision, action: 'start' }); return aiRequest<AiJob>(`/jobs/${job.id}`); })}>{t('ai.acceptProposal')}</button>}</>}
    {job.kind === 'product' && content && <>
      <details open><summary>{t('ai.contentReview')}</summary><div className="ai-columns"><div><h3>{t('ai.originalFeed')}</h3>{job.facts?.map(f => <details key={f.id} open={job.facts?.length === 1}><summary>{f.name} · {f.variant_attributes?.map(a => a.value).join(' / ')}</summary><div className="ai-original">{f.description}</div></details>)}</div>
        <div>{(['title', 'short_description', 'long_description', 'seo_title', 'meta_description', 'h1_descriptor', 'future_name', 'h1_descr_suffix'] as const).map(field => <label key={field}>{t(`ai.fields.${field}`)} · {content[field]?.length || 0}
          <textarea className={field === 'long_description' ? 'ai-long-text' : ''} disabled={!editable || busy} value={content[field] || ''} onChange={e => { setContent({ ...content, [field]: e.target.value }); setDirty(true); }} /></label>)}</div></div></details>
      <details><summary>{t('ai.renderedDescription')}</summary><iframe title={t('ai.renderedDescription')} sandbox="" srcDoc={`<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><style>body{font:15px/1.6 system-ui;padding:20px;color:#222}table{border-collapse:collapse}td,th{padding:8px;border:1px solid #ccc}</style></head><body>${content.long_description}</body></html>`} /></details>
      <details><summary>{t('ai.parametersAndEvidence')}</summary><label>{t('ai.parameterValues')}<textarea className="ai-code" disabled={!editable || busy} value={parameters} onChange={e => { setParameters(e.target.value); setDirty(true); }} /></label><div className="ai-scroll"><table><thead><tr><th>{t('ai.claim')}</th><th>{t('ai.source')}</th><th>{t('ai.evidence')}</th></tr></thead><tbody>{content.evidence?.map((e, i) => <tr key={i}><td>{e.claim}</td><td>{e.source}</td><td>{e.quote}</td></tr>)}</tbody></table></div>
        <label>{t('ai.missingFacts')}<textarea disabled={!editable || busy} value={(content.missing_facts || []).join('\n')} onChange={e => { setContent({ ...content, missing_facts: e.target.value.split('\n').filter(Boolean) }); setDirty(true); }} /></label></details>
      {editable && <div className="ai-actions"><button disabled={busy || !dirty} onClick={() => save(false)}>{t('ai.saveContent')}</button><button className="ai-primary" disabled={busy} onClick={() => save(true)}>{t('ai.approveContent')}</button>{dirty && <small>{t('ai.unsaved')}</small>}</div>}
    </>}
    {job.preview && <div className="ai-actions"><button disabled={busy || dirty} onClick={() => setImportOpen(true)}>{t(job.import_result ? 'ai.showImportResult' : 'ai.showImportPreview')}</button></div>}
    {!['generating', 'importing', 'completed', 'exists', 'uncertain', 'cancelled'].includes(job.status) && <button disabled={busy} onClick={() => action('cancel')}>{t('ai.cancelJob')}</button>}
    <details><summary>{t('ai.history')}</summary>{job.events?.map((e, i) => <p key={i}><time>{new Date(e.at).toLocaleString()}</time> · {t(`ai.states.${e.status}`, { defaultValue: e.status })} · {e.note}</p>)}</details>
    {error && <div role="alert" className="ai-notice ai-error">{error}</div>}
    {importOpen && job.preview && <CatalogImport key={job.preview.preview_id} preview={job.preview} result={job.import_result || null} shopName={job.shop} sending={busy} error={error}
      visibilityLabel={t(job.policy.active_after_import ? 'ai.activeAfterImport' : 'ai.hiddenAfterImport')}
      onClose={() => setImportOpen(false)} onConfirm={() => action('import')} onRetry={() => action('retry_import')}
      onExclude={() => { setError(t('ai.reselectPartial')); }}
      onReprice={overrides => perform(() => aiRequest<AiJob>(`/jobs/${job.id}/prices`, { expected_revision: job.revision, sale_price_overrides: overrides }))} />}
  </section>;
}
