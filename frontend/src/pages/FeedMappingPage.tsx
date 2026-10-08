import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ArrowLeft, Eye, Plus, RefreshCw, Save, Trash2, Upload } from 'lucide-react';
import { Button } from '../components/ui/Button.new';
import { CategoryTree } from '../components/product/CategoryTree';
import { useUnsavedNavigationGuard } from '../hooks/useUnsavedNavigationGuard';
import { accessRevision, hubRequest, hubUnlocked, subscribeAccess } from '../api/access';
import { CatalogStatus, TargetOptions } from '../api/catalog';
import {
  applySavedFeedMapping, emptyFeedDefinition, FeedBinding, FeedDefinition, FeedInspection,
  FeedMappingConfig, FeedPreview, FeedTransform, getFeedMapping, inspectFeed,
  previewFeedMapping, saveFeedMapping, uploadFeedSample,
} from '../api/feedMapping';
import './FeedMappingPage.css';

const display = (value: unknown): string => value == null ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value);
const normalizeDefinition = (value?: FeedDefinition): FeedDefinition => ({ ...emptyFeedDefinition(), ...value, bindings: value?.bindings || [], category_rules: value?.category_rules || [] });

export function FeedMappingPage() {
  const { supplier = '' } = useParams<{ supplier: string }>();
  const { t, i18n } = useTranslation();
  const f = (key: string, options?: Record<string, unknown>) => t(`feedMapping.${key}`, options);
  const access = useSyncExternalStore(subscribeAccess, accessRevision);
  const unlocked = hubUnlocked();
  const [status, setStatus] = useState<CatalogStatus | null>(null);
  const [feedKey, setFeedKey] = useState('products');
  const [shop, setShop] = useState('');
  const [config, setConfig] = useState<FeedMappingConfig | null>(null);
  const [definition, setDefinition] = useState<FeedDefinition>(emptyFeedDefinition);
  const [inspection, setInspection] = useState<FeedInspection | null>(null);
  const [targets, setTargets] = useState<TargetOptions | null>(null);
  const [preview, setPreview] = useState<FeedPreview | null>(null);
  const [busy, setBusy] = useState('');
  const [previewBusy, setPreviewBusy] = useState(false);
  const [error, setError] = useState('');
  const [previewError, setPreviewError] = useState('');
  const [notice, setNotice] = useState('');
  const [step, setStep] = useState<'source' | 'fields' | 'categories'>('source');
  const [sourceFilter, setSourceFilter] = useState('');
  const [automaticPreview, setAutomaticPreview] = useState(false);
  const uploadRef = useRef<HTMLInputElement>(null);
  const generation = useRef(0);
  const dirty = config !== null && JSON.stringify(definition) !== JSON.stringify(normalizeDefinition(config.definition));
  useUnsavedNavigationGuard(dirty, f('discard'));
  const sourceDefinition = shop ? normalizeDefinition(config?.inherited_definition) : definition;
  const sourceFields = inspection?.fields || [];
  const sampleId = inspection?.sample_id;

  function failure(e: unknown) {
    const err = e as { code?: string; message?: string };
    setError(err.code?.includes('conflict') || err.code?.includes('revision') ? f('conflict') : err.code === 'feed_sample_missing' ? f('sampleMissing') : err.message || f('failed'));
  }
  async function execute(name: string, action: () => Promise<void>) {
    setBusy(name); setError(''); setNotice('');
    try { await action(); } catch (e) { failure(e); } finally { setBusy(''); }
  }
  function change(next: FeedDefinition) { generation.current += 1; setPreviewBusy(false); setDefinition(next); setNotice(''); setPreview(null); setPreviewError(''); }
  function binding(index: number, next: FeedBinding) { change({ ...definition, bindings: definition.bindings.map((row, i) => i === index ? next : row) }); }
  function switchContext(action: () => void, preserveSample = false) {
    if (!dirty || window.confirm(f('discard'))) { generation.current += 1; setPreviewBusy(false); if (!preserveSample) setInspection(null); setPreview(null); setConfig(null); setError(''); setNotice(''); action(); }
  }
  useEffect(() => {
    if (!unlocked) return;
    const controller = new AbortController();
    void hubRequest<CatalogStatus>(`/api/suppliers/${encodeURIComponent(supplier)}/catalog`, undefined, controller.signal)
      .then(setStatus).catch(e => { if (!controller.signal.aborted) failure(e); });
    return () => controller.abort();
  }, [supplier, unlocked, access]);
  useEffect(() => {
    if (!unlocked) return;
    const controller = new AbortController();
    generation.current += 1; setPreviewBusy(false); setBusy('loading'); setTargets(null); setConfig(null); setError(''); setPreview(null);
    void (async () => {
      try {
        const saved = await getFeedMapping(supplier, feedKey, shop, controller.signal);
        if (controller.signal.aborted) return;
        setConfig(saved); setDefinition(normalizeDefinition(saved.definition));
        if (shop) {
          const options = await hubRequest<TargetOptions>(`/api/shops/${encodeURIComponent(shop)}/import/options`, undefined, controller.signal);
          if (!controller.signal.aborted) setTargets(options);
        }
      } catch (e) { if (!controller.signal.aborted) failure(e); }
      finally { if (!controller.signal.aborted) setBusy(''); }
    })();
    return () => controller.abort();
  }, [supplier, feedKey, shop, unlocked, access]);
  async function loadSource(file?: File) {
    generation.current += 1; setPreviewBusy(false);
    await execute('inspect', async () => {
      const data = file ? await uploadFeedSample(supplier, feedKey, file, sourceDefinition) : await inspectFeed(supplier, feedKey, sourceDefinition, sampleId);
      setInspection(data); setPreview(null); setNotice(f('inspected', { count: data.total_records }));
    });
  }
  async function runPreview(signal?: AbortSignal) {
    const current = ++generation.current;
    setPreviewBusy(true); setPreviewError('');
    try {
      const result = await previewFeedMapping(supplier, feedKey, shop, definition, sampleId, signal);
      if (!signal?.aborted && current === generation.current) setPreview(result);
    } catch (e) { if (!signal?.aborted && current === generation.current) setPreviewError((e as Error).message || f('failed')); }
    finally { if (current === generation.current) setPreviewBusy(false); }
  }
  useEffect(() => {
    if (!automaticPreview || !inspection || busy) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => { void runPreview(controller.signal); }, 700);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [automaticPreview, inspection, definition, shop, feedKey, busy]);
  const leafCategories = targets?.categories.map(category => ({ ...category, assignable: category.assignable !== false && category.active !== false && !targets.categories.some(child => child.parent_code === category.code && child.active !== false) })) || [];
  const sources = status?.sources || [];
  const transformed = (row: FeedBinding) => row.transforms || [];
  const sourceExample = (source?: string) => sourceFields.find(field => field.path === source)?.examples.map(display).join(' · ') || '';
  const targetLabel = (key: string, fallback?: string) => f(`targets.${key.replace(/\./g, '_')}`, { defaultValue: fallback || key });
  function previewCategory(item: FeedPreview['items'][number]) {
    let code = display((item as unknown as Record<string, unknown>).target_category_code);
    if (!code) return item.category || item.category_code || f('empty');
    const path: string[] = [], seen = new Set<string>();
    while (code && !seen.has(code)) {
      seen.add(code); const category = targets?.categories.find(row => row.code === code);
      path.unshift(category?.names[i18n.language] || category?.names.sk || code); code = category?.parent_code || '';
    }
    return path.join(' / ');
  }
  const sharedOnly = new Set(['code', 'group_code', 'group_name', 'eans', 'supplier_stock', 'supplier_stock_min', 'supplier_stock_external', 'supplier_stock_raw', 'supplier_stock_external_raw', 'supplier_external_available', 'availability']);
  const fieldOptions = (config?.fields || []).filter(field => shop ? !sharedOnly.has(field.key) : field.key !== 'target_category_code');
  const appendBinding = (target = '') => change({ ...definition, bindings: [...definition.bindings, { target: target || fieldOptions.find(field => !definition.bindings.some(row => row.target === field.key))?.key || 'name', source: '', transforms: [] }] });
  function addCategoryRules() {
    const known = new Set(definition.category_rules.map(rule => rule.source));
    change({ ...definition, category_rules: [...definition.category_rules, ...(inspection?.source_categories || []).filter(source => !known.has(source)).map(source => ({ source, target_code: '' }))] });
  }

  return <main className="feed-mapping">
    <header className="fm-header"><div><Link to="/suppliers"><ArrowLeft size={15} />{f('back')}</Link><h1>{f('title')}</h1><p>{status?.name || supplier} · {f('subtitle')}</p></div>
      <Link to={`/suppliers/${encodeURIComponent(supplier)}/catalog`}>{f('catalog')}</Link>
    </header>
    {!unlocked ? <div className="fm-card"><p>{f('loginRequired')}</p><Link to="/login">{t('accounts.login')}</Link></div> : <>
      {error && <p className="fm-alert fm-error" role="alert">{error}</p>}
      {notice && <p className="fm-alert" role="status">{notice}</p>}
      <div className="fm-context fm-card"><label>{f('feed')}<select value={feedKey} disabled={!!busy} onChange={e => switchContext(() => setFeedKey(e.target.value))}>{sources.length ? sources.map(source => <option key={source.key} value={source.key}>{source.name || source.key}</option>) : <option value="products">products</option>}</select></label>
        <label>{f('scope')}<select aria-label={f('scope')} value={shop} disabled={!!busy} onChange={e => switchContext(() => setShop(e.target.value), true)}><option value="">{f('baseScope')}</option>{status?.shops.map(item => <option key={item.code} value={item.code}>{item.name}</option>)}</select></label>
        <div className="fm-revision"><strong>{config?.configured ? f('savedRevision', { revision: config.revision }) : f('notConfigured')}</strong><span>{dirty ? f('unsaved') : f('savedState')}</span>{shop && <small>{f('inherited', { revision: config?.base_revision || 0 })}</small>}</div>
        <Button variant="secondary" disabled={!!busy || !config} icon={<RefreshCw size={15} />} onClick={() => { if (!dirty || window.confirm(f('discard'))) void execute('reload', async () => { const current = await getFeedMapping(supplier, feedKey, shop); setConfig(current); setDefinition(normalizeDefinition(current.definition)); setPreview(null); }); }}>{f('reload')}</Button>
      </div>
      <p className="fm-hint">{shop ? f('shopScopeHelp') : f('baseScopeHelp')}</p>
      <nav className="fm-tabs" aria-label={f('steps')}>{(['source', 'fields', 'categories'] as const).map((key, index) => <button key={key} type="button" aria-current={step === key ? 'step' : undefined} onClick={() => setStep(key)}><span>{index + 1}</span>{f(`steps_${key}`)}{key === 'fields' && <small>{definition.bindings.length}</small>}{key === 'categories' && <small>{definition.category_rules.length}</small>}</button>)}</nav>
      {config && <>
        {step === 'source' && <section className="fm-card"><h2>{f('sourceTitle')}</h2><p>{f('sourceHelp')}</p>
          <div className="fm-actions"><Button variant="secondary" disabled={!!busy} loading={busy === 'inspect'} icon={<Eye size={16} />} onClick={() => loadSource()}>{inspection ? f('inspectAgain') : f('inspectCurrent')}</Button><Button variant="secondary" disabled={!!busy} icon={<Upload size={16} />} onClick={() => uploadRef.current?.click()}>{f('upload')}</Button><input ref={uploadRef} type="file" accept=".xml,.csv,.tsv,.json,.txt" hidden onChange={e => { const file = e.target.files?.[0]; if (file) void loadSource(file); e.target.value = ''; }} /></div>
          <details className="fm-advanced"><summary>{f('parserSettings')}</summary><p>{shop ? f('parserBaseOnly') : f('parserHelp')}</p><fieldset disabled={!!busy || !!shop} className="fm-grid"><label>{f('format')}<select value={sourceDefinition.format} onChange={e => change({ ...definition, format: e.target.value as FeedDefinition['format'] })}>{['auto', 'xml', 'csv', 'json'].map(format => <option key={format} value={format}>{format === 'auto' ? f('detect') : format.toUpperCase()}</option>)}</select></label><label>{f('recordPath')}<input value={sourceDefinition.record_path} placeholder={f('recordPathExample')} onChange={e => change({ ...definition, record_path: e.target.value })} /></label><label>{f('delimiter')}<input value={sourceDefinition.csv_delimiter} placeholder={f('detect')} maxLength={1} onChange={e => change({ ...definition, csv_delimiter: e.target.value })} /></label><label>{f('encoding')}<select value={sourceDefinition.csv_encoding} onChange={e => change({ ...definition, csv_encoding: e.target.value as FeedDefinition['csv_encoding'] })}><option value="utf-8-sig">UTF-8</option><option value="cp1250">Windows-1250</option><option value="iso-8859-2">ISO-8859-2</option></select></label></fieldset></details>
          {inspection && <><div className="fm-source-meta"><strong>{inspection.format.toUpperCase()}</strong><span>{f('recordCount', {count: inspection.total_records})}</span><span>{f('fieldCount', {count: sourceFields.length})}</span><code>{inspection.record_path}</code>{sampleId && <span>{f('uploadedSample')}</span>}</div><label className="fm-search">{f('searchFields')}<input type="search" value={sourceFilter} onChange={e => setSourceFilter(e.target.value)} /></label><div className="fm-table-scroll"><table><thead><tr><th>{f('sourceField')}</th><th>{f('type')}</th><th>{f('coverage')}</th><th>{f('examples')}</th></tr></thead><tbody>{sourceFields.filter(field => field.path.toLowerCase().includes(sourceFilter.toLowerCase())).map(field => <tr key={field.path}><td><code>{field.path}</code></td><td>{field.type}</td><td>{field.populated} / {field.total}</td><td className="fm-example">{field.examples.map(display).join(' · ') || f('empty')}</td></tr>)}</tbody></table></div><div className="fm-actions"><Button onClick={() => setStep('fields')}>{f('continueFields')}</Button></div></>}
        </section>}
        {step === 'fields' && <section className="fm-card"><h2>{f('fieldsTitle')}</h2><p>{config.native_parser ? f('nativeHelp') : f('genericHelp')}</p>{shop && <p className="fm-hint">{f('overlayHelp')}</p>}
          {!inspection && <p className="fm-alert">{f('inspectHint')} <button onClick={() => setStep('source')}>{f('steps_source')}</button></p>}
          <datalist id="fm-source-paths">{sourceFields.map(field => <option key={field.path} value={field.path}>{field.examples.map(display).join(' · ').slice(0, 140)}</option>)}</datalist>
          <fieldset disabled={!!busy} className="fm-bindings"><div className="fm-table-scroll"><table className="fm-binding-table"><thead><tr><th>{f('targetField')}</th><th>{f('valueSource')}</th><th>{f('sampleAndRules')}</th><th>{f('actions')}</th></tr></thead><tbody>{definition.bindings.map((row, index) => {
            const special = row.target.startsWith('parameter:') ? 'parameter:' : row.target.startsWith('meta:') ? 'meta:' : null;
            const mode = row.constant !== null && row.constant !== undefined ? 'constant' : 'source';
            return <tr key={index}><td><select aria-label={f('targetForRow', {row: index + 1})} value={special || row.target} onChange={e => binding(index, {...row, target: e.target.value})}>{fieldOptions.map(field => <option key={field.key} value={field.key}>{targetLabel(field.key, field.label)}</option>)}{!fieldOptions.some(field => field.key === 'parameters') && <option value="parameters">{f('parameterList')}</option>}<option value="parameter:">{f('oneParameter')}</option><option value="meta:">{f('customField')}</option></select>{special && <input aria-label={f('targetName')} placeholder={f(special === 'parameter:' ? 'parameterName' : 'customFieldName')} value={row.target.slice(special.length)} onChange={e => binding(index, {...row, target: `${special}${e.target.value}`})} />}</td>
              <td><select aria-label={f('sourceMode')} value={mode} onChange={e => { const next = {...row}; if (e.target.value === 'constant') { delete next.source; next.constant = ''; } else { delete next.constant; next.source = ''; } binding(index, next); }}><option value="source">{f('fromFeed')}</option><option value="constant">{f('fixedValue')}</option></select><input aria-label={f('sourceForRow', { row: index + 1 })} list={mode === 'source' ? 'fm-source-paths' : undefined} value={display(mode === 'source' ? row.source : row.constant)} onChange={e => binding(index, {...row, ...(mode === 'source' ? {source: e.target.value} : {constant: e.target.value})})} placeholder={mode === 'source' ? f('chooseSource') : f('enterValue')} />{['parameters','variant_attributes'].includes(row.target) && <div className="fm-parameter-paths"><label>{f('parameterNamePath')}<input value={row.param_name_path || ''} onChange={e => binding(index, {...row, param_name_path: e.target.value})} placeholder="DESC" /></label><label>{f('parameterValuePath')}<input value={row.param_value_path || ''} onChange={e => binding(index, {...row, param_value_path: e.target.value})} placeholder="VAL" /></label></div>}</td>
              <td><small className="fm-example">{mode === 'source' ? sourceExample(row.source) || f('noExample') : display(row.constant)}</small><details><summary>{f('transformations')} {transformed(row).length > 0 && `(${transformed(row).length})`}</summary><label>{f('fallback')}<input value={display(row.default)} onChange={e => { const next = {...row}; if (e.target.value === '') delete next.default; else next.default = e.target.value; binding(index, next); }} placeholder={f('noFallback')} /></label><TransformEditor value={transformed(row)} onChange={transforms => binding(index, {...row, transforms})} /></details></td>
              <td><button className="fm-icon-button" type="button" title={f('removeBinding')} aria-label={f('removeRow', { row: index + 1 })} onClick={() => change({...definition, bindings: definition.bindings.filter((_, i) => i !== index)})}><Trash2 size={17} /></button></td></tr>;
          })}</tbody></table></div>{!definition.bindings.length && <p className="fm-empty">{f('noBindings')}</p>}
            <div className="fm-actions"><Button variant="secondary" icon={<Plus size={15} />} onClick={() => appendBinding()}>{f('addField')}</Button><Button variant="secondary" icon={<Plus size={15} />} onClick={() => appendBinding('parameter:')}>{f('addParameter')}</Button></div>
            <label className="fm-checkbox"><input type="checkbox" checked={definition.seo_fallback} onChange={e => change({...definition, seo_fallback: e.target.checked})} />{f('seoFallback')}</label><p className="fm-hint">{f('seoHelp')}</p>
          </fieldset>
        </section>}
        {step === 'categories' && <section className="fm-card"><h2>{f('categoriesTitle')}</h2><p>{f('categoriesHelp')}</p>{!shop ? <p className="fm-alert">{f('chooseShop')}</p> : <><fieldset disabled={!!busy}><div className="fm-actions"><Button variant="secondary" onClick={addCategoryRules} disabled={!inspection?.source_categories.length}>{f('addDetectedCategories')}</Button><Button variant="secondary" icon={<Plus size={15} />} onClick={() => change({...definition, category_rules: [...definition.category_rules, {source:'',target_code:''}]})}>{f('addCategory')}</Button></div><datalist id="fm-source-categories">{inspection?.source_categories.map(source => <option value={source} key={source} />)}</datalist><div className="fm-table-scroll"><table className="fm-category-table"><thead><tr><th>{f('supplierCategory')}</th><th>{f('targetCategory')}</th><th>{f('actions')}</th></tr></thead><tbody>{definition.category_rules.map((rule, index) => <tr key={index}><td><input aria-label={f('supplierCategoryRow', {row:index+1})} list="fm-source-categories" value={rule.source} onChange={e => change({...definition, category_rules: definition.category_rules.map((item,i) => i === index ? {...item, source:e.target.value} : item)})} /></td><td><CategoryTree categories={leafCategories} value={rule.target_code} language={i18n.language} onChange={target_code => change({...definition, category_rules: definition.category_rules.map((item,i) => i === index ? {...item,target_code} : item)})} /></td><td><button className="fm-icon-button" type="button" aria-label={f('removeRow', {row:index+1})} onClick={() => change({...definition,category_rules:definition.category_rules.filter((_,i)=>i!==index)})}><Trash2 size={17} /></button></td></tr>)}</tbody></table></div>{!definition.category_rules.length && <p className="fm-empty">{f('noCategories')}</p>}</fieldset></>}</section>}
        <section className="fm-card fm-preview"><div className="fm-section-heading"><div><h2>{f('previewTitle')}</h2><p>{f('previewHelp')}</p></div><div className="fm-actions"><label className="fm-checkbox"><input type="checkbox" checked={automaticPreview} onChange={e => setAutomaticPreview(e.target.checked)} />{f('autoPreview')}</label><Button variant="secondary" icon={<Eye size={15} />} disabled={!!busy || !inspection || previewBusy} loading={previewBusy} onClick={() => runPreview()}>{f('preview')}</Button></div></div>
          {previewError && <p className="fm-alert fm-error" role="alert">{previewError}</p>}
          {!preview && <p className="fm-empty">{inspection ? f('previewStale') : f('previewNeedsSource')}</p>}
          {preview && <>{preview.errors.length > 0 && <div className="fm-alert fm-error" role="alert">{preview.errors.map((item,index) => <p key={index}>{f('previewRowError', {row: item.row, message:item.message})}</p>)}</div>}<div className="fm-table-scroll"><table><thead><tr><th>{targetLabel('name')}</th><th>{targetLabel('eans')}</th><th>{targetLabel('brand')}</th><th>{targetLabel('prices.purchase_net')}</th><th>{targetLabel('prices.retail_gross')}</th><th>{f('categoryAndSeo')}</th><th>{f('details')}</th></tr></thead><tbody>{preview.items.map((item,index) => <tr key={item.code || index}><td><strong>{item.name}</strong><small>{item.code}</small></td><td>{item.eans?.join(', ')}</td><td>{item.brand}</td><td>{item.prices?.purchase_net ?? '—'} {item.prices?.currency}</td><td>{item.prices?.retail_gross ?? '—'} {item.prices?.currency}</td><td>{previewCategory(item)}<small>{display((item as unknown as Record<string,unknown>).seo_title)}</small></td><td><details><summary>{f('showValues')}</summary><dl className="fm-product-values">{Object.entries(item).filter(([key]) => !['source_xml', 'source_fields', 'manufacturer_description'].includes(key)).map(([key,value]) => <React.Fragment key={key}><dt>{targetLabel(key)}</dt><dd>{display(value) || f('empty')}</dd></React.Fragment>)}</dl></details></td></tr>)}</tbody></table></div></>}
        </section>
        <footer className="fm-save-bar"><div><strong>{dirty ? f('unsaved') : f('savedState')}</strong><small>{f('saveHelp')}</small></div><Button disabled={!!busy || (!dirty && config.configured)} loading={busy === 'save'} icon={<Save size={16} />} onClick={() => execute('save', async () => { const saved = await saveFeedMapping(supplier, feedKey, shop, config.revision, definition); setConfig(saved); setDefinition(normalizeDefinition(saved.definition)); setNotice(f('saved')); })}>{f('save')}</Button></footer>
        {!shop && config.configured && <details className="fm-card fm-apply"><summary>{f('applyTitle')}</summary><p>{f('applyHelp')}</p><Button variant="secondary" disabled={!!busy || dirty || !preview || !!preview.errors.length} loading={busy === 'apply'} onClick={() => execute('apply', async () => { await applySavedFeedMapping(supplier, feedKey, config.revision, sampleId); setNotice(f('applied')); })}>{f('apply')}</Button></details>}
      </>}
    </>}
  </main>;
}

function TransformEditor({value,onChange}:{value:FeedTransform[];onChange:(value:FeedTransform[])=>void}) {
  const {t} = useTranslation();
  const f = (key:string) => t(`feedMapping.${key}`);
  const ops:FeedTransform['op'][] = ['trim','strip_html','split','join','replace','decimal','multiply','round','truncate','map','prefix','suffix'];
  const update = (index:number, next:FeedTransform) => onChange(value.map((row,i)=>i===index?next:row));
  return <div className="fm-transforms">{value.map((row,index)=><div className="fm-transform" key={index}><div className="fm-transform-line"><select aria-label={f('transformation')} value={row.op} onChange={e=>update(index,{op:e.target.value as FeedTransform['op']})}>{ops.map(op=><option key={op} value={op}>{f(`operations.${op}`)}</option>)}</select>{!['trim','strip_html','decimal','map'].includes(row.op) && <input aria-label={f('transformValue')} value={row.value || ''} onChange={e=>update(index,{...row,value:e.target.value})} />}{row.op==='replace' && <input aria-label={f('replaceWith')} value={row.with || ''} onChange={e=>update(index,{...row,with:e.target.value})} />}<button type="button" className="fm-icon-button" aria-label={f('removeTransformation')} onClick={()=>onChange(value.filter((_,i)=>i!==index))}><Trash2 size={14}/></button></div>{row.op==='map' && <ValueMap value={row.values || {}} onChange={values=>update(index,{...row,values})}/>}</div>)}<button type="button" className="fm-text-button" onClick={()=>onChange([...value,{op:'trim'}])}>+ {f('addTransformation')}</button></div>;
}
function ValueMap({value,onChange}:{value:Record<string,string>;onChange:(value:Record<string,string>)=>void}) {
  const {t}=useTranslation();
  const [duplicate, setDuplicate] = useState(false);
  const entries=Object.entries(value);
  return <div className="fm-value-map">{duplicate && <small role="alert">{t('feedMapping.duplicateValue')}</small>}{entries.map(([source,target],index)=><div key={index}><input aria-label={t('feedMapping.originalValue')} value={source} onChange={e=>{if(e.target.value!==source && Object.prototype.hasOwnProperty.call(value,e.target.value)){setDuplicate(true);return;}setDuplicate(false);const next=[...entries];next[index]=[e.target.value,target];onChange(Object.fromEntries(next));}}/><span>→</span><input aria-label={t('feedMapping.mappedValue')} value={target} onChange={e=>onChange({...value,[source]:e.target.value})}/><button type="button" className="fm-icon-button" aria-label={t('feedMapping.removeValue')} onClick={()=>onChange(Object.fromEntries(entries.filter((_,i)=>i!==index)))}><Trash2 size={14}/></button></div>)}<button type="button" className="fm-text-button" disabled={Object.prototype.hasOwnProperty.call(value,'')} onClick={()=>onChange({...value,'':''})}>+ {t('feedMapping.addValue')}</button><small>{t('feedMapping.unmatchedValues')}</small></div>;
}
