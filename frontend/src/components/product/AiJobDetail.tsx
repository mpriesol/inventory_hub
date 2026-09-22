import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AiContent, AiJob, aiRequest } from '../../api/aiContent';
import { AiParameterValues } from './AiParameterValues';
import { AiPolicyFields } from './AiPolicyFields';
import { CatalogImport } from './CatalogImport';

function DescriptionPreview({ value, title }: { value: string; title: string }) {
  return <iframe title={title} sandbox="" srcDoc={`<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><style>body{font:15px/1.6 system-ui;padding:20px;color:#222}table{border-collapse:collapse}td,th{padding:8px;border:1px solid #ccc}</style></head><body>${value}</body></html>`} />;
}

type PreviewRow = Record<string, unknown>;
const previewRows = (value: unknown): PreviewRow[] => Array.isArray(value) ? value.filter(row => row && typeof row === 'object') : [];
function hasPreviewValue(data: PreviewRow, field: string, language: string) {
  return (['title','short_description','long_description','seo_title','seo_description'].includes(field)
    ? previewRows(data.descriptions).find(row => row.language === language)?.[field] : data[field]) != null;
}
function UpdateValue({ data, field, language, title }: { data: PreviewRow; field: string; language: string; title: string }) {
  const { t } = useTranslation();
  const localized = (value: unknown) => previewRows(value).find(row => row.language === language) || {};
  if (['title', 'short_description', 'long_description', 'seo_title', 'seo_description'].includes(field)) {
    const value = String(localized(data.descriptions)[field] ?? '');
    return field.endsWith('description') && !field.startsWith('seo_') && value
      ? <DescriptionPreview title={title} value={value} />
      : <p className="ai-original">{value || '—'}</p>;
  }
  if (['metas','parameters','categories'].includes(field) && !previewRows(data[field]).length) return <p>—</p>;
  if (field === 'metas') return <dl>{previewRows(data.metas).filter(row => ['h1_descriptor', 'future_name', 'h1_descr_suffix'].includes(String(row.key))).map(row => <React.Fragment key={String(row.key)}><dt>{t(`ai.fields.${row.key}`)}</dt><dd>{String(row.value ?? localized(row.values).value ?? (row.values as PreviewRow | undefined)?.[language] ?? '—')}</dd></React.Fragment>)}</dl>;
  if (field === 'parameters') return <dl>{previewRows(data.parameters).map((row, i) => <React.Fragment key={i}><dt>{String(localized(row.descriptions).name ?? row.name ?? '—')}</dt><dd>{previewRows(row.values).map(value => String(localized(value.descriptions).value ?? value.value ?? '')).filter(Boolean).join(' | ') || '—'}</dd></React.Fragment>)}</dl>;
  if (field === 'categories') return <ul>{previewRows(data.categories).map(row => <li key={String(row.code)}>{String(row.code)}{row.main_yn ? ` · ${t('ai.mainCategory')}` : ''}</li>)}</ul>;
  return <p>{data[field] == null ? '—' : String(data[field])}</p>;
}

export function AiJobDetail({ job, onChange }: { job: AiJob; onChange: (job: AiJob) => void }) {
  const { t } = useTranslation();
  const [content, setContent] = useState<AiContent | undefined>(job.kind === 'product' ? job.output : undefined);
  const [parameters, setParameters] = useState<AiContent['parameters']>(job.kind === 'product' ? job.output?.parameters || [] : []);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [selectedIds, setSelectedIds] = useState<number[]>(job.product_ids);
  const [updateFields, setUpdateFields] = useState(['title','short_description','long_description','seo_title','seo_description','metas']);
  const [importOpen, setImportOpen] = useState(false);
  const updateState = job.update_preview?.state || job.update_state;
  const pendingUpdate = ['sending','uncertain'].includes(updateState || '');
  const editable = !pendingUpdate && ['review', 'blocked', 'ready'].includes(job.status) && job.kind === 'product';
  async function perform(fn: () => Promise<AiJob>) { setBusy(true); setError(''); try { const updated = await fn(); onChange(updated); setDirty(false); } catch (e) { setError(t(`ai.errors.${(e as Error & {code?: string}).code}`, {defaultValue:(e as Error).message})); } finally { setBusy(false); } }
  const importErrors = [...new Set([...(job.preview?.errors || []), ...(job.import_result?.errors || []), ...(job.import_result?.items || job.preview?.items || []).flatMap(i => i.errors)])];
  const unknownImport = job.import_result?.items.some(i => i.status === 'uncertain');
  const updateNeedsAttention = ['sending','uncertain','rejected'].includes(updateState || '');
  const mismatchedFields = job.update_result?.status === 'uncertain' ? job.update_result.mismatched_fields || [] : [];
  const canFork = !pendingUpdate && job.kind === 'product' && !job.update_only && !unknownImport && !['queued','generating','preparing_import','import_queued','importing'].includes(job.status);
  const action = (action: string) => perform(() => aiRequest<AiJob>(`/jobs/${job.id}/action`, { action, expected_revision: job.revision }));
  const save = (approve: boolean) => perform(() => aiRequest<AiJob>(`/jobs/${job.id}/review`, { expected_revision: job.revision, approve, content: { ...content, parameters } }));
  return <section className="ai-card">
    <div className="ai-row"><h2 className="ai-grow">{job.name} · {job.shop}</h2><span className="ai-badge">{t(updateNeedsAttention ? `ai.updateStates.${updateState}` : job.update_only && job.status === 'exists' ? updateState === 'completed' ? 'ai.updatedExisting' : 'ai.readyForUpdate' : `ai.states.${job.status}`, { defaultValue: job.status })}</span></div>
    <p>{job.code} · {t('ai.publishedVersion', { version: job.rules_version })} · {job.category_profile}</p>
    <div className="ai-next-step"><strong>{t('ai.nextStep')}</strong><p>{t(updateNeedsAttention ? `ai.updateStates.${updateState}` : job.update_only ? `ai.updateNext.${updateState === 'completed' ? 'completed' : job.status}` : `ai.next.${job.status}`, { defaultValue: t(`ai.next.${job.status}`, {defaultValue:job.status}) })}</p></div>
    {importErrors.length > 0 && <div className="ai-notice ai-error" role="alert"><strong>{t('ai.importBlockers')}</strong>{importErrors.map(v => <p key={v}>{t(`catalog.codes.${v}`, { defaultValue: t(`ai.errors.${v}`, { defaultValue: v }) })}</p>)}{unknownImport && <p>{t('ai.unknownImportHelp')}</p>}</div>}
    {!job.update_only && ['import_failed','import_blocked'].includes(job.status) && <button disabled={busy} onClick={() => action('retry_import')}>{t(unknownImport ? 'ai.reconcileImport' : 'ai.retryImport')}</button>}
    {!pendingUpdate && ['import_blocked','exists','completed','cancelled'].includes(job.status) && job.output && <button disabled={busy} onClick={() => action('reopen')}>{t('ai.reopenContent')}</button>}
    {job.update_only ? <p className="ai-notice">{t('ai.updateOnlyHelp')}</p> : <AiPolicyFields value={job.policy} origins={job.origins} />}
    {job.applied_rules && <details><summary>{t('ai.appliedRules')}</summary><p>{t('ai.appliedRulesHelp')} · {t('ai.publishedVersion', {version:job.rules_version})}</p>{job.applied_rules.map(rule => <details key={rule.id}><summary>{rule.name}</summary><pre className="ai-original">{rule.text}</pre></details>)}{job.source_kind !== 'shop' && job.resolved_import_policy && <><h3>{t('ai.importPolicy')}</h3><dl>{(['orderable','unknown','supplier_name'] as const).map(key => <React.Fragment key={key}><dt>{t(`ai.importPolicyFields.${key}`)}</dt><dd>{job.resolved_import_policy?.[key] || '—'}</dd></React.Fragment>)}<dt>{t('ai.hideZeroStock')}</dt><dd>{job.resolved_import_policy.hide_zero_stock == null ? '—' : t(job.resolved_import_policy.hide_zero_stock ? 'ai.on' : 'ai.off')}</dd></dl></>}<h3>{t('ai.parameterRegistry')}</h3>{job.parameter_registry?.length ? <ul>{job.parameter_registry.map(parameter => <li key={parameter.name}><strong>{parameter.name}</strong> · {t(`ai.${parameter.scope}`)}{parameter.required ? ` · ${t('ai.required')}` : ''}{parameter.values.length ? ` · ${parameter.values.join(' | ')}` : ''}</li>)}</ul> : <p>{t('ai.noRegisteredParameters')}</p>}</details>}
    {job.policy.show_cost_estimate !== false && <p>{t('ai.estimate')}: {Number(job.estimate_usd).toFixed(3)} USD · {t('ai.actualCost')}: {job.actual_usd === null ? '—' : `${Number(job.actual_usd).toFixed(4)} USD`}</p>}
    {job.status === 'estimate' && <div className="ai-actions"><button disabled={busy} className="ai-primary" onClick={() => action('start')}>{t('ai.startProcessing')}</button><small>{t('ai.estimateHelp')}</small></div>}
    {job.error && <div className="ai-notice ai-error">{t(`ai.errors.${job.error}`, { defaultValue: job.error })}</div>}
    {job.checks?.errors?.map(v => <div key={v} className="ai-notice ai-error">{t(`ai.errors.${v.split(':')[0]}`, { defaultValue: v })}{v.includes(':') ? ` · ${v.split(':').slice(1).join(':')}` : ''}</div>)}
    {!!job.checks?.warnings?.length && <h3>{t('ai.nonBlockingWarnings')}</h3>}
    {job.checks?.warnings?.map(v => <div key={v} className="ai-notice">{t(`ai.errors.${v}`, { defaultValue: v })}</div>)}
    {job.kind === 'rules' && job.output?.instructions && <><p>{job.output.reason}</p><pre className="ai-original">{job.output.instructions}</pre>{job.output.parameters && <details open><summary>{t('ai.parameterRegistry')}</summary><div className="ai-scroll"><table><thead><tr><th>{t('ai.parameterName')}</th><th>{t('ai.required')}</th><th>{t('ai.parameterScope')}</th><th>{t('ai.allowedValues')}</th></tr></thead><tbody>{job.output.parameters.map(p => <tr key={p.name}><td>{p.name}</td><td>{t(p.required ? 'ai.on' : 'ai.off')}</td><td>{t(`ai.${p.scope}`)}</td><td>{p.values.join(' | ')}</td></tr>)}</tbody></table></div></details>}{job.output.questions?.map(q => <p key={q}>{q}</p>)}{job.status === 'review' && <button disabled={busy} onClick={() => perform(async () => { await aiRequest(`/jobs/${job.id}/accept-proposal`, { expected_revision: job.revision, action: 'start' }); return aiRequest<AiJob>(`/jobs/${job.id}`); })}>{t('ai.acceptProposal')}</button>}</>}
    {job.kind === 'product' && content && <>
      <details open><summary>{t('ai.contentReview')}</summary><div className="ai-columns"><div><h3>{t(job.source_kind === 'shop' ? 'ai.originalShop' : 'ai.originalFeed')}</h3>{job.facts?.map(f => <details key={f.id} open={job.facts?.length === 1}><summary>{f.name} · {f.variant_attributes?.map(a => a.value).join(' / ')}</summary><div className="ai-original">{f.description}</div></details>)}</div>
        <div>{(['title', 'short_description', 'long_description', 'seo_title', 'meta_description', 'h1_descriptor', 'future_name', 'h1_descr_suffix'] as const).map(field => <label key={field}>{t(`ai.fields.${field}`)} · {content[field]?.length || 0}
          <textarea className={field === 'long_description' ? 'ai-long-text' : ''} disabled={!editable || busy} value={content[field] || ''} onChange={e => { setContent({ ...content, [field]: e.target.value }); setDirty(true); }} /></label>)}</div></div></details>
      <details><summary>{t('ai.renderedDescription')}</summary><DescriptionPreview title={t('ai.renderedDescription')} value={content.long_description} /></details>
      <details open={job.checks?.errors?.some(error => error.includes('parameter') || error.includes('evidence')) || undefined}><summary>{t('ai.parametersAndEvidence')}</summary><AiParameterValues values={parameters} registry={job.parameter_registry || []} facts={job.facts || []} disabled={!editable || busy} onChange={values => { setParameters(values); setDirty(true); }} /><div className="ai-scroll"><table><thead><tr><th>{t('ai.claim')}</th><th>{t('ai.source')}</th><th>{t('ai.evidence')}</th><th /></tr></thead><tbody>{content.evidence?.map((e, i) => <tr key={i}><td>{e.claim}</td><td>{e.source}</td><td>{e.quote}</td><td><button type="button" disabled={!editable || busy} aria-label={`${t('ai.removeEvidence')} ${e.claim}`} onClick={() => { setContent({ ...content, evidence: content.evidence.filter((_, index) => index !== i) }); setDirty(true); }}>{t('ai.removeEvidence')}</button></td></tr>)}</tbody></table></div><p>{t('ai.removeEvidenceHelp')}</p>
        <label>{t('ai.missingFacts')}<textarea disabled={!editable || busy} value={(content.missing_facts || []).join('\n')} onChange={e => { setContent({ ...content, missing_facts: e.target.value.split('\n').filter(Boolean) }); setDirty(true); }} /></label></details>
      {editable && <div className="ai-actions"><button disabled={busy || !dirty} onClick={() => save(false)}>{t('ai.saveContent')}</button><button className="ai-primary" disabled={busy} onClick={() => save(true)}>{t(job.update_only ? 'ai.approveUpdateContent' : 'ai.approveContent')}</button>{dirty && <small>{t('ai.unsaved')}</small>}</div>}
    </>}
    {job.kind === 'product' && content && ['review','blocked','ready','exists','completed','import_blocked'].includes(job.status) && <details open={job.update_only || undefined}><summary>{t('ai.updateExisting')}</summary><p>{t('ai.updateExistingHelp')}</p><div className="ai-toolbar">{['title','short_description','long_description','seo_title','seo_description','metas','parameters','categories','availability'].map(field => <label className="ai-check" key={field}><input type="checkbox" disabled={busy || pendingUpdate || (field === 'availability' && job.source_kind === 'shop')} checked={updateFields.includes(field)} onChange={e => setUpdateFields(old => e.target.checked ? [...old,field] : old.filter(f => f !== field))} />{t(`ai.updateFields.${field}`)}</label>)}</div>{job.source_kind === 'shop' && <p>{t('ai.shopAvailabilityHelp')}</p>}<button disabled={busy || pendingUpdate || dirty || !updateFields.length} onClick={() => perform(() => aiRequest(`/jobs/${job.id}/update-preview`, {expected_revision:job.revision,fields:updateFields}))}>{t('ai.prepareUpdate')}</button></details>}
    {job.update_preview && <div className="ai-card">
      <h3>{t('ai.updateComparison')}</h3><p>{t(`ai.updateStates.${job.update_preview.state}`)}</p>
      {job.update_preview.availability_basis === 'supplier_unset_shop_stock' && <p className="ai-notice">{t('ai.supplierAvailabilityUnsetStock')}</p>}
      {(job.update_result?.error || mismatchedFields.length > 0) && <div className="ai-notice ai-error" role="alert">
        {job.update_result?.error && <p>{t(`ai.errors.${job.update_result.error}`, {defaultValue:job.update_result.error})}</p>}
        {mismatchedFields.length > 0 && <><strong>{t('ai.mismatchedUpdateFields')}</strong><ul>{mismatchedFields.map(field => <li key={field}>{t(`ai.updateFields.${field}`, {defaultValue:field})}</li>)}</ul><p>{t('ai.updateReadbackHelp')}</p></>}
      </div>}
      {job.update_preview.fields.map(field => <details key={field} open={mismatchedFields.includes(field) || undefined}>
        <summary>{t(`ai.updateFields.${field}`)}</summary><div className="ai-grid">
          <div><h4>{t('ai.beforeUpdate')}</h4><UpdateValue data={job.update_preview!.before} field={field} language={job.options?.language || 'sk'} title={`${t('ai.beforeUpdate')} · ${t(`ai.updateFields.${field}`)}`} /></div>
          <div><h4>{t('ai.afterUpdate')}</h4><UpdateValue data={job.update_preview!.after} field={field} language={job.options?.language || 'sk'} title={`${t('ai.afterUpdate')} · ${t(`ai.updateFields.${field}`)}`} /></div>
          {mismatchedFields.includes(field) && <div><h4>{t('ai.actualShopValue')}</h4>{!job.update_result?.observed || !hasPreviewValue(job.update_result.observed, field, job.options?.language || 'sk')
            ? <p>{t('ai.readbackValueMissing')}</p>
            : <UpdateValue data={job.update_result.observed} field={field} language={job.options?.language || 'sk'} title={`${t('ai.actualShopValue')} · ${t(`ai.updateFields.${field}`)}`} />}</div>}
        </div>
      </details>)}
      {!['completed','rejected','sending'].includes(job.update_preview.state) && <button disabled={busy || dirty} className="ai-primary" onClick={() => perform(() => aiRequest(`/jobs/${job.id}/update-confirm`, {expected_revision:job.revision,preview_id:job.update_preview!.id}))}>{t(job.update_preview.state === 'ready' ? 'ai.confirmUpdate' : 'ai.reconcileUpdate')}</button>}
    </div>}
    {canFork && <details><summary>{t('ai.prepareAgain')}</summary><p>{t('ai.partialHelp')}</p>{job.facts?.map(f => <label className="ai-check" key={f.id}><input type="checkbox" checked={selectedIds.includes(f.id)} onChange={e => setSelectedIds(old => e.target.checked ? [...old,f.id] : old.filter(id => id !== f.id))} />{f.name} · {f.variant_attributes?.map(a => a.value).join(' / ')}</label>)}<div className="ai-actions">{job.output && <button disabled={busy || !selectedIds.length} onClick={() => perform(() => aiRequest(`/jobs/${job.id}/fork`,{expected_revision:job.revision,product_ids:selectedIds,reuse_content:true,use_ai:true}))}>{t('ai.copySelectedContent')}</button>}<button disabled={busy || !selectedIds.length} onClick={() => perform(() => aiRequest(`/jobs/${job.id}/fork`,{expected_revision:job.revision,product_ids:selectedIds,reuse_content:false,use_ai:true}))}>{t('ai.newAiPreparation')}</button><button disabled={busy || !selectedIds.length} onClick={() => perform(() => aiRequest(`/jobs/${job.id}/fork`,{expected_revision:job.revision,product_ids:selectedIds,reuse_content:false,use_ai:false}))}>{t('ai.useOriginalSelected')}</button></div></details>}
    {!job.update_only && job.preview && <div className="ai-actions"><button disabled={busy || dirty} onClick={() => setImportOpen(true)}>{t(job.import_result ? 'ai.showImportResult' : 'ai.showImportPreview')}</button></div>}
    {!pendingUpdate && !['generating', 'importing', 'completed', 'exists', 'uncertain', 'cancelled'].includes(job.status) && <button disabled={busy} onClick={() => action('cancel')}>{t('ai.cancelJob')}</button>}
    <details><summary>{t('ai.history')}</summary>{job.events?.map((e, i) => <p key={i}><time>{new Date(e.at).toLocaleString()}</time> · {t(`ai.states.${e.status}`, { defaultValue: e.status })} · {e.note}</p>)}</details>
    {error && <div role="alert" className="ai-notice ai-error">{error}</div>}
    {!job.update_only && importOpen && job.preview && <CatalogImport key={job.preview.preview_id} preview={job.preview} result={job.import_result || null} shopName={job.shop} sending={busy} error={error}
      visibilityLabel={t(job.policy.active_after_import ? 'ai.activeAfterImport' : 'ai.hiddenAfterImport')}
      onClose={() => setImportOpen(false)} onConfirm={() => action('import')} onRetry={() => action('retry_import')}
      onExclude={ids => perform(() => aiRequest(`/jobs/${job.id}/fork`, {expected_revision:job.revision,product_ids:job.product_ids.filter(id => !ids.includes(id)),reuse_content:true,use_ai:job.use_ai}))}
      onReprice={overrides => perform(() => aiRequest<AiJob>(`/jobs/${job.id}/prices`, { expected_revision: job.revision, sale_price_overrides: overrides }))} />}
  </section>;
}
