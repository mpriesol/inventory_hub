import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ArrowLeft, ArrowRight, Check, Eye, GripVertical, Plus, RefreshCw, Save, Trash2, Upload } from 'lucide-react';
import { Button } from '../components/ui/Button.new';
import { CategoryTree } from '../components/product/CategoryTree';
import { useUnsavedNavigationGuard } from '../hooks/useUnsavedNavigationGuard';
import { accessRevision, hubRequest, hubUnlocked, subscribeAccess } from '../api/access';
import { CatalogStatus, TargetOptions } from '../api/catalog';
import {
  applySavedFeedMapping, emptyFeedDefinition, FeedBinding, FeedDefinition, FeedInspection,
  FeedMappingConfig, FeedParameter, FeedParameterOptions, FeedPreview, FeedTransform,
  getFeedMapping, getFeedParameterOptions, inspectFeed, previewFeedMapping, saveFeedMapping, uploadFeedSample,
} from '../api/feedMapping';
import './FeedMappingPage.css';

const display = (value: unknown): string => value == null ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value);
const normalizeDefinition = (value?: FeedDefinition): FeedDefinition => ({ ...emptyFeedDefinition(), ...value, bindings: value?.bindings || [], category_rules: value?.category_rules || [] });
const searchText = (value: string) => value.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
const matches = (value: string, query: string) => searchText(value).includes(searchText(query));
const parserKey = (value: FeedDefinition) => JSON.stringify([value.format,value.record_path,value.csv_delimiter,value.csv_encoding]);
const dragType = 'application/x-hub-feed-mapping';

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
  const [parameterOptions, setParameterOptions] = useState<FeedParameterOptions | null>(null);
  const [parameterError, setParameterError] = useState('');
  const [preview, setPreview] = useState<FeedPreview | null>(null);
  const [busy, setBusy] = useState('');
  const [previewBusy, setPreviewBusy] = useState(false);
  const [error, setError] = useState('');
  const [previewError, setPreviewError] = useState('');
  const [notice, setNotice] = useState('');
  const [step, setStep] = useState<'source' | 'fields' | 'categories' | 'parameters'>('categories');
  const [sourceFilter, setSourceFilter] = useState('');
  const [categoryFilter, setCategoryFilter] = useState('');
  const [targetFilter, setTargetFilter] = useState('');
  const [unmappedOnly, setUnmappedOnly] = useState(false);
  const [selectedCategory, setSelectedCategory] = useState('');
  const [selectedField, setSelectedField] = useState('');
  const [newFieldTarget, setNewFieldTarget] = useState('name');
  const [selectedParameter, setSelectedParameter] = useState('');
  const [parameterFilter, setParameterFilter] = useState('');
  const [parameterTargetFilter, setParameterTargetFilter] = useState('');
  const [parameterProfile, setParameterProfile] = useState('');
  const [automaticPreview, setAutomaticPreview] = useState(false);
  const uploadRef = useRef<HTMLInputElement>(null);
  const previewRef = useRef<HTMLElement>(null);
  const generation = useRef(0);
  const contextEpoch = useRef(0);
  const inspectionOwner = useRef('');
  const inspectionSettings = useRef('');
  const sourceOwner = `${supplier}/${feedKey}`;
  const dirty = config !== null && JSON.stringify(definition) !== JSON.stringify(normalizeDefinition(config.definition));
  useUnsavedNavigationGuard(dirty, f('discard'));
  const sourceDefinition = shop ? normalizeDefinition(config?.inherited_definition) : definition;
  const sourceFields = inspection?.fields || [];
  const sourceParameters = inspection?.source_parameters || [];
  const sampleId = inspection?.sample_id;
  const targetLabel = (key: string, fallback?: string) => f(`targets.${key.replace(/\./g, '_')}`, { defaultValue: fallback || key });
  const fieldLabel = (path: string) => {
    const last = path.split('/').slice(-1)[0] || path;
    const alias: Record<string, string> = { PRODUCT: 'name', PRODUCTNAME: 'name', ITEM_NAME: 'name', NAME: 'name', ITEM_ID: 'code', ITEM_CODE: 'code', SKU: 'code', EAN: 'eans', EAN13: 'eans', DESCRIPTION: 'description', SHORT_DESCRIPTION: 'short_description', CATEGORYTEXT: 'category', CATEGORYID: 'category_code', MANUFACTURER: 'brand', BRAND: 'brand', IMGURL: 'images', URL: 'url', PRICE_VAT: 'prices.retail_gross' };
    if (last.toUpperCase()==='PRICE' || last.toUpperCase()==='PRICE_VAT') return f('rawPrice');
    return alias[last.toUpperCase()] ? targetLabel(alias[last.toUpperCase()]) : last.replace(/[_-]+/g, ' ');
  };
  function failure(e: unknown) {
    const err = e as { code?: string; message?: string };
    setError(err.code?.includes('conflict') || err.code?.includes('revision') ? f('conflict') : err.code === 'feed_sample_missing' ? f('sampleMissing') : err.message || f('failed'));
  }
  async function execute(name: string, action: (current: () => boolean) => Promise<void>) {
    const epoch=contextEpoch.current;
    const current=()=>epoch===contextEpoch.current;
    generation.current+=1; setPreviewBusy(false);
    setBusy(name); setError(''); setNotice('');
    try { await action(current); } catch (e) { if(current())failure(e); } finally { if(current())setBusy(''); }
  }
  function change(next: FeedDefinition) { generation.current += 1; setPreviewBusy(false); setDefinition(next); setNotice(''); setPreview(null); setPreviewError(''); }
  function binding(index: number, next: FeedBinding) { change({ ...definition, bindings: definition.bindings.map((row, i) => i === index ? next : row) }); }
  function switchContext(action: () => void, preserveSample = false) {
    if (busy) return;
    if (!dirty || window.confirm(f('discard'))) {
      generation.current += 1; setPreviewBusy(false);
      if (!preserveSample) { setInspection(null); setSelectedField(''); setSelectedParameter(''); }
      setSelectedCategory(''); setParameterProfile(''); setPreview(null); setConfig(null); setError(''); setNotice(''); action();
    }
  }
  useEffect(() => {
    if (!unlocked) return;
    const controller = new AbortController();
    setStatus(null);
    void hubRequest<CatalogStatus>(`/api/suppliers/${encodeURIComponent(supplier)}/catalog`, undefined, controller.signal)
      .then(data=>{if(!controller.signal.aborted)setStatus(data);}).catch(e => { if (!controller.signal.aborted) failure(e); });
    return () => controller.abort();
  }, [supplier, unlocked, access]);
  useEffect(() => {
    if (!unlocked) return;
    const controller = new AbortController();
    contextEpoch.current += 1;
    generation.current += 1; setPreviewBusy(false); setBusy('loading'); setTargets(null); setParameterOptions(null); setParameterError(''); setConfig(null); setError(''); setPreview(null);
    void (async () => {
      try {
        const saved = await getFeedMapping(supplier, feedKey, shop, controller.signal);
        if (controller.signal.aborted) return;
        setConfig(saved); setDefinition(normalizeDefinition(saved.definition));
        const jobs: Promise<void>[] = [];
        const parser=normalizeDefinition(shop?saved.inherited_definition:saved.definition);
        const sameSource=inspectionOwner.current===sourceOwner;
        if (!sameSource) { setInspection(null); setSelectedCategory(''); setSelectedField(''); setSelectedParameter(''); }
        if (!inspection || !sameSource || inspectionSettings.current!==parserKey(parser)) jobs.push(inspectFeed(supplier, feedKey, parser, sameSource?inspection?.sample_id:undefined, controller.signal)
          .then(data => { if (!controller.signal.aborted) { inspectionOwner.current = sourceOwner; inspectionSettings.current=parserKey(parser); setInspection(data); } }).catch(e => { if (!controller.signal.aborted) failure(e); }));
        if (shop) jobs.push(hubRequest<TargetOptions>(`/api/shops/${encodeURIComponent(shop)}/import/options`, undefined, controller.signal)
          .then(options => { if (!controller.signal.aborted) setTargets(options); }).catch(e => { if (!controller.signal.aborted) failure(e); }));
        jobs.push(getFeedParameterOptions(supplier, feedKey, shop, controller.signal)
          .then(options => { if (!controller.signal.aborted) setParameterOptions(options); }).catch(() => { if (!controller.signal.aborted) setParameterError(f('parameterLoadFailed')); }));
        await Promise.all(jobs);
      } catch (e) { if (!controller.signal.aborted) failure(e); }
      finally { if (!controller.signal.aborted) setBusy(''); }
    })();
    return () => {controller.abort();contextEpoch.current+=1;generation.current+=1;};
  }, [supplier, feedKey, shop, unlocked, access]);
  async function loadSource(file?: File) {
    generation.current += 1; setPreviewBusy(false);
    await execute('inspect', async current => {
      const data = file ? await uploadFeedSample(supplier, feedKey, file, sourceDefinition) : await inspectFeed(supplier, feedKey, sourceDefinition, sampleId);
      if(!current())return;
      inspectionOwner.current = sourceOwner; inspectionSettings.current=parserKey(sourceDefinition); setInspection(data); setSelectedCategory(''); setSelectedField(''); setSelectedParameter(''); setPreview(null); setNotice(f('inspected', { count: data.total_records }));
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

  const categoryParents = new Set((targets?.categories || []).filter(category=>category.active!==false).map(category=>category.parent_code));
  const leafCategories = targets?.categories.map(category => ({ ...category, assignable: category.assignable !== false && category.active !== false && !categoryParents.has(category.code) })) || [];
  const sources = status?.sources || [];
  const transformed = (row: FeedBinding) => row.transforms || [];
  const sourceExample = (source?: string) => sourceFields.find(field => field.path === source)?.examples.map(display).join(' · ') || '';
  const categoryIndex = new Map((targets?.categories || []).map(row=>[row.code,row]));
  function categoryPath(code: string) {
    const path: string[] = [], seen = new Set<string>();
    while (code && !seen.has(code)) {
      seen.add(code); const category = categoryIndex.get(code);
      path.unshift(category?.names[i18n.language] || category?.names.sk || Object.values(category?.names || {})[0] || code); code = category?.parent_code || '';
    }
    return path.join(' / ');
  }
  function previewCategory(item: FeedPreview['items'][number]) {
    const code = display((item as unknown as Record<string, unknown>).target_category_code);
    return code ? categoryPath(code) : item.category || item.category_code || f('empty');
  }
  const sharedOnly = new Set(['code', 'group_code', 'group_name', 'eans', 'supplier_stock', 'supplier_stock_min', 'supplier_stock_external', 'supplier_stock_raw', 'supplier_stock_external_raw', 'supplier_external_available', 'availability']);
  const fieldOptions = (config?.fields || []).filter(field => shop ? !sharedOnly.has(field.key) : field.key !== 'target_category_code');
  const effectiveNewFieldTarget = fieldOptions.some(field=>field.key===newFieldTarget) ? newFieldTarget : fieldOptions[0]?.key || 'name';
  function appendBinding(target = '', source = '') {
    if (busy) return;
    target = target || fieldOptions.find(field => !definition.bindings.some(row => row.target === field.key))?.key || 'name';
    const existing = definition.bindings.find(row=>row.target===target);
    if (existing && !window.confirm(f('replaceField',{name:targetLabel(target)}))) return;
    change({...definition,bindings:[...definition.bindings.filter(row=>row.target!==target),{target,source,transforms:[]}]});
  }
  function assignField(index: number, source: string) {
    if (busy || !sourceFields.some(field => field.path === source)) return;
    const next = { ...definition.bindings[index], source }; delete next.constant; delete next.param_match_name; delete next.param_name_path; delete next.param_value_path;
    binding(index, next);
  }
  function startDrag(e: React.DragEvent, kind: string, value: string) { e.dataTransfer.setData(dragType, JSON.stringify({ kind, value })); e.dataTransfer.effectAllowed = 'copy'; }
  function dropValue(e: React.DragEvent, kind: string) {
    e.preventDefault();
    try { const data = JSON.parse(e.dataTransfer.getData(dragType)); return data.kind === kind && typeof data.value === 'string' ? data.value : ''; } catch { return ''; }
  }
  const detectedCategories = inspection?.source_category_options?.length ? inspection.source_category_options : (inspection?.source_categories || []).map(source => ({source, label:source, count:0}));
  const sourceCategories = [...detectedCategories, ...definition.category_rules.filter(rule => !detectedCategories.some(item => item.source === rule.source)).map(rule => ({source:rule.source,label:rule.source,count:0}))];
  const categoryRule = (source: string) => definition.category_rules.find(rule => rule.source === source);
  const filteredCategories = sourceCategories.filter(item => matches(`${item.label} ${item.source}`, categoryFilter) && (!unmappedOnly || !categoryRule(item.source)?.target_code));
  const categoryTargets = leafCategories.filter(category => category.assignable && matches(categoryPath(category.code), targetFilter));
  function mapCategory(source: string, target_code: string) {
    if (busy || !sourceCategories.some(item => item.source === source) || !leafCategories.some(item => item.code === target_code && item.assignable)) return;
    change({ ...definition, category_rules: [...definition.category_rules.filter(rule => rule.source !== source), {source, target_code}] });
    setSelectedCategory(source);
  }
  const parameterKey = (parameter: FeedParameter) => JSON.stringify([parameter.source, parameter.name, parameter.param_name_path, parameter.param_value_path]);
  const selectedSourceParameter = sourceParameters.find(parameter => parameterKey(parameter) === selectedParameter);
  const parameterTargets = new Map<string, {name:string; origin:string; values?:string[]; required?:boolean; unit?:string}>();
  const parameterLanguage=targets?.languages?.find(language=>language.default)?.code || 'sk';
  if (!parameterProfile) for (const item of parameterOptions?.parameters || []) {
    const name = item.names[parameterLanguage] || item.names.sk || Object.values(item.names)[0];
    if (name) parameterTargets.set(name, {name,origin:'Upgates'});
  }
  for (const profile of parameterOptions?.category_profiles || []) if (!parameterProfile || profile.id === parameterProfile) for (const item of profile.parameters) {
    const previous = parameterTargets.get(item.name);
    parameterTargets.set(item.name, {name:item.name,origin:previous ? `${previous.origin} · ${profile.name}` : profile.name,values:item.values,required:item.required,unit:item.unit});
  }
  function mapParameter(key: string, name: string) {
    const source = sourceParameters.find(item => parameterKey(item) === key);
    if (busy || !source || !parameterTargets.has(name)) return;
    const target = `parameter:${name}`;
    const existing = definition.bindings.find(row => row.target === target);
    if (existing && !window.confirm(f('replaceParameter', { name }))) return;
    const row: FeedBinding = {target, source:source.source, param_match_name:source.name, param_name_path:source.param_name_path,param_value_path:source.param_value_path,transforms:[]};
    change({...definition,bindings:[...definition.bindings.filter(item => item.target !== target),row]});
    setNotice(f('parameterConnected', {source:source.name,target:name}));
  }
  const nativeActive = !!config?.native_parser && ['auto','xml'].includes(sourceDefinition.format)
    && (!inspection || inspection.format==='xml') && (!sourceDefinition.record_path.trim()
      || !config.native_record_paths || config.native_record_paths.includes(sourceDefinition.record_path.trim().replace(/^\/+|\/+$/g,'')));
  const requiredCategoryTarget = inspection?.category_fields?.code_path ? 'category_code' : 'category';
  const categorySourceBound = nativeActive || [...(shop ? config?.inherited_definition?.bindings || [] : definition.bindings)].some(row => row.target===requiredCategoryTarget);
  const categoryFieldSuggestions = [
    {target:'category_code',source:inspection?.category_fields?.code_path},
    {target:'category',source:inspection?.category_fields?.label_path},
  ].filter(item=>item.source && !definition.bindings.some(row=>row.target===item.target));
  function connectCategoryFields() {
    if (shop || busy) return;
    change({...definition,bindings:[...definition.bindings,...categoryFieldSuggestions.map(item=>({target:item.target,source:item.source,transforms:[{op:'trim' as const}]}))]});
  }
  const visibleFields = sourceFields.filter(field => matches(`${fieldLabel(field.path)} ${field.path} ${field.examples.map(display).join(' ')}`, sourceFilter));

  function renderBindings(parameters = false) {
    const rows = definition.bindings.map((row,index)=>({row,index})).filter(({row}) => row.target.startsWith('parameter:') === parameters);
    return <div className="fm-binding-cards">{rows.map(({row,index}) => {
      const special = row.target.startsWith('parameter:') ? 'parameter:' : row.target.startsWith('meta:') ? 'meta:' : null;
      const mode = row.constant !== null && row.constant !== undefined ? 'constant' : 'source';
      return <article className="fm-binding-card" key={index} onDragOver={e=>e.preventDefault()} onDrop={e=>{const source=dropValue(e,'field');if(source)assignField(index,source);}}>
        <div className="fm-binding-heading"><strong>{special ? row.target.slice(special.length) || f('oneParameter') : targetLabel(row.target)}</strong><button className="fm-icon-button" type="button" aria-label={f('removeRow',{row:index+1})} onClick={()=>change({...definition,bindings:definition.bindings.filter((_,i)=>i!==index)})}><Trash2 size={16}/></button></div>
        <div className="fm-binding-grid"><label>{f('targetField')}<select aria-label={f('targetForRow',{row:index+1})} value={special || row.target} onChange={e=>{const next={...row,target:e.target.value};if(!next.target.startsWith('parameter:')){delete next.param_match_name;delete next.param_name_path;delete next.param_value_path;}binding(index,next);}}>{fieldOptions.map(field=><option key={field.key} value={field.key}>{targetLabel(field.key,field.label)}</option>)}{!fieldOptions.some(field=>field.key==='parameters') && <option value="parameters">{f('parameterList')}</option>}<option value="parameter:">{f('oneParameter')}</option><option value="meta:">{f('customField')}</option></select>{special && <input aria-label={f('targetName')} placeholder={f(special==='parameter:'?'parameterName':'customFieldName')} value={row.target.slice(special.length)} onChange={e=>binding(index,{...row,target:`${special}${e.target.value}`})}/>}</label>
          <div><label>{f('valueSource')}<select aria-label={f('sourceMode')} value={mode} onChange={e=>{const next={...row};delete next.param_match_name;delete next.param_name_path;delete next.param_value_path;if(e.target.value==='constant'){delete next.source;next.constant='';}else{delete next.constant;next.source='';}binding(index,next);}}><option value="source">{f('fromFeed')}</option><option value="constant">{f('fixedValue')}</option></select></label>
            {mode==='constant' ? <input aria-label={f('sourceForRow',{row:index+1})} value={display(row.constant)} onChange={e=>binding(index,{...row,constant:e.target.value})} placeholder={f('enterValue')}/> : row.param_match_name ? <div className="fm-linked-value"><Check size={15}/><span>{row.param_match_name}<small>{row.source}</small></span></div> : <select aria-label={f('sourceForRow',{row:index+1})} value={row.source || ''} onChange={e=>assignField(index,e.target.value)}><option value="">{f('chooseSource')}</option>{row.source && !sourceFields.some(field=>field.path===row.source) && <option value={row.source}>{row.source}</option>}{sourceFields.map(field=><option key={field.path} value={field.path}>{fieldLabel(field.path)} · {field.examples.map(display).join(' · ').slice(0,80)} ({field.path})</option>)}</select>}
            {selectedField && !parameters && <button type="button" className="fm-text-button" onClick={()=>assignField(index,selectedField)}>{f('useSelectedField',{name:fieldLabel(selectedField)})}</button>}
          </div></div>
        <small className="fm-example">{row.param_match_name ? sourceParameters.find(item=>item.source===row.source && item.name===row.param_match_name)?.examples.join(' · ') : mode==='source' ? sourceExample(row.source) || f('noExample') : display(row.constant)}</small>
        <details className="fm-binding-advanced"><summary>{f('transformations')} {transformed(row).length>0 && `(${transformed(row).length})`}</summary><label>{f('fallback')}<input value={display(row.default)} onChange={e=>{const next={...row};if(e.target.value==='')delete next.default;else next.default=e.target.value;binding(index,next);}} placeholder={f('noFallback')}/></label><TransformEditor value={transformed(row)} onChange={transforms=>binding(index,{...row,transforms})}/></details>
        <details className="fm-binding-advanced"><summary>{f('advancedBinding')}</summary>{mode==='source' && <label>{f('sourceField')}<input aria-label={f('manualSourceForRow',{row:index+1})} value={row.source || ''} onChange={e=>binding(index,{...row,source:e.target.value})}/></label>}{(['parameters','variant_attributes'].includes(row.target) || !!row.param_match_name) && <div className="fm-parameter-paths"><label>{f('parameterNamePath')}<input value={row.param_name_path || ''} onChange={e=>binding(index,{...row,param_name_path:e.target.value})}/></label><label>{f('parameterValuePath')}<input value={row.param_value_path || ''} onChange={e=>binding(index,{...row,param_value_path:e.target.value})}/></label></div>}{!!row.param_match_name && <label>{f('exactParameterName')}<input value={row.param_match_name} onChange={e=>binding(index,{...row,param_match_name:e.target.value})}/></label>}</details>
      </article>;
    })}{!rows.length && <p className="fm-empty">{f(parameters?'noParameterBindings':'noBindings')}</p>}</div>;
  }

  return <main className="feed-mapping">
    <header className="fm-header"><div><Link to="/suppliers"><ArrowLeft size={15}/>{f('back')}</Link><h1>{f('title')}</h1><p>{status?.name || supplier} · {f('subtitle')}</p></div><Link to={`/suppliers/${encodeURIComponent(supplier)}/catalog`}>{f('catalog')}</Link></header>
    {!unlocked ? <div className="fm-card"><p>{f('loginRequired')}</p><Link to="/login">{t('accounts.login')}</Link></div> : <>
      {error && <p className="fm-alert fm-error" role="alert">{error}</p>}{notice && <p className="fm-alert" role="status">{notice}</p>}
      <div className="fm-context fm-card"><label>{f('feed')}<select value={feedKey} disabled={!!busy} onChange={e=>switchContext(()=>setFeedKey(e.target.value))}>{sources.length ? sources.map(source=><option key={source.key} value={source.key}>{source.name || source.key}</option>) : <option value="products">products</option>}</select></label><label>{f('scope')}<select aria-label={f('scope')} value={shop} disabled={!!busy} onChange={e=>switchContext(()=>setShop(e.target.value),true)}><option value="">{f('baseScope')}</option>{status?.shops.map(item=><option key={item.code} value={item.code}>{item.name}</option>)}</select></label>
        <div className="fm-revision"><strong>{config?.configured ? f('savedRevision',{revision:config.revision}) : f('notConfigured')}</strong><span>{dirty?f('unsaved'):f('savedState')}</span>{shop && <small>{f('inherited',{revision:config?.base_revision || 0})}</small>}</div>
        <Button variant="secondary" disabled={!!busy || !config} icon={<RefreshCw size={15}/>} onClick={()=>{if(!dirty || window.confirm(f('discard')))void execute('reload',async active=>{setPreview(null);const current=await getFeedMapping(supplier,feedKey,shop);if(!active())return;setConfig(current);setDefinition(normalizeDefinition(current.definition));});}}>{f('reload')}</Button>
      </div>
      <p className="fm-hint">{shop?f('shopScopeHelp'):f('baseScopeHelp')}</p>
      <div className="fm-workflow"><span className={inspection?'fm-complete':''}><b>{inspection?<Check size={14}/>:1}</b>{inspection?f('sourceReady',{count:inspection.total_records}):f('sourceLoading')}</span><ArrowRight size={15}/><span><b>2</b>{f('connectData')}</span><ArrowRight size={15}/><button type="button" onClick={()=>previewRef.current?.scrollIntoView({behavior:'smooth',block:'start'})}><b>3</b>{f('reviewAndSave')}</button></div>
      <nav className="fm-tabs" aria-label={f('steps')}>{(['categories','fields','parameters','source'] as const).map(key=><button key={key} type="button" aria-current={step===key?'step':undefined} onClick={()=>setStep(key)}>{f(`steps_${key}`)}<small>{key==='categories'?`${definition.category_rules.filter(rule=>rule.target_code).length} / ${sourceCategories.length}`:key==='fields'?definition.bindings.filter(row=>!row.target.startsWith('parameter:')).length:key==='parameters'?definition.bindings.filter(row=>row.target.startsWith('parameter:')).length:sourceFields.length}</small></button>)}</nav>
      {inspection && inspectionSettings.current!==parserKey(sourceDefinition) && <p className="fm-alert">{f('sourceSettingsChanged')} <button type="button" disabled={!!busy} onClick={()=>loadSource()}>{f('inspectAgain')}</button></p>}
      {busy==='loading' && <p className="fm-hint" role="status">{f('loadingWorkspace')}</p>}
      {config && <>
        {step==='source' && <section className="fm-card"><h2>{f('sourceTitle')}</h2><p>{f('sourceHelp')}</p><div className="fm-actions"><Button variant="secondary" disabled={!!busy} loading={busy==='inspect'} icon={<Eye size={16}/>} onClick={()=>loadSource()}>{inspection?f('inspectAgain'):f('inspectCurrent')}</Button><Button variant="secondary" disabled={!!busy} icon={<Upload size={16}/>} onClick={()=>uploadRef.current?.click()}>{f('upload')}</Button><input ref={uploadRef} type="file" accept=".xml,.csv,.tsv,.json,.txt" hidden onChange={e=>{const file=e.target.files?.[0];if(file)void loadSource(file);e.target.value='';}}/></div>
          <details className="fm-advanced"><summary>{f('parserSettings')}</summary><p>{shop?f('parserBaseOnly'):f('parserHelp')}</p><fieldset disabled={!!busy || !!shop} className="fm-grid"><label>{f('format')}<select value={sourceDefinition.format} onChange={e=>change({...definition,format:e.target.value as FeedDefinition['format']})}>{['auto','xml','csv','json'].map(format=><option key={format} value={format}>{format==='auto'?f('detect'):format.toUpperCase()}</option>)}</select></label><label>{f('recordPath')}<input value={sourceDefinition.record_path} placeholder={f('recordPathExample')} onChange={e=>change({...definition,record_path:e.target.value})}/></label><label>{f('delimiter')}<input value={sourceDefinition.csv_delimiter} placeholder={f('detect')} maxLength={1} onChange={e=>change({...definition,csv_delimiter:e.target.value})}/></label><label>{f('encoding')}<select value={sourceDefinition.csv_encoding} onChange={e=>change({...definition,csv_encoding:e.target.value as FeedDefinition['csv_encoding']})}><option value="utf-8-sig">UTF-8</option><option value="cp1250">Windows-1250</option><option value="iso-8859-2">ISO-8859-2</option></select></label></fieldset></details>
          {inspection && <><div className="fm-source-meta"><strong>{inspection.format.toUpperCase()}</strong><span>{f('recordCount',{count:inspection.total_records})}</span><span>{f('fieldCount',{count:sourceFields.length})}</span><span>{f('categoryCount',{count:detectedCategories.length})}</span><span>{f('parameterCount',{count:sourceParameters.length})}</span><code>{inspection.record_path}</code>{sampleId && <span>{f('uploadedSample')}</span>}</div><label className="fm-search">{f('searchFields')}<input type="search" value={sourceFilter} onChange={e=>setSourceFilter(e.target.value)}/></label><div className="fm-table-scroll"><table><thead><tr><th>{f('sourceField')}</th><th>{f('type')}</th><th>{f('coverage')}</th><th>{f('examples')}</th></tr></thead><tbody>{visibleFields.map(field=><tr key={field.path}><td><strong>{fieldLabel(field.path)}</strong><small><code>{field.path}</code></small></td><td>{field.type}</td><td>{field.populated} / {field.total}</td><td className="fm-example">{field.examples.map(display).join(' · ') || f('empty')}</td></tr>)}</tbody></table></div><div className="fm-actions"><Button onClick={()=>setStep('fields')}>{f('continueFields')}</Button></div></>}
        </section>}
        {step==='categories' && <section className="fm-card"><h2>{f('categoriesTitle')}</h2><p>{f('categoriesHelp')}</p>{!shop && <div className="fm-alert"><p>{f('chooseShop')}</p><div className="fm-actions">{status?.shops.map(item=><Button key={item.code} variant="secondary" disabled={!!busy} onClick={()=>switchContext(()=>setShop(item.code),true)}>{item.name}</Button>)}</div></div>}
          {!categorySourceBound && <p className="fm-alert">{f('categorySourceRequired')} <button type="button" onClick={()=>{if(shop)switchContext(()=>{setShop('');setStep('fields');},true);else setStep('fields');}}>{f('connectCategoryField')}</button></p>}
          <p className="fm-hint">{f('categoryConnectHelp')}</p><fieldset disabled={!!busy} className="fm-workbench"><div className="fm-pane"><div className="fm-pane-heading"><h3>{f('supplierCategory')}</h3><span>{sourceCategories.length}</span></div><label>{f('searchSupplierCategories')}<input type="search" value={categoryFilter} onChange={e=>setCategoryFilter(e.target.value)}/></label><label className="fm-checkbox fm-filter-check"><input type="checkbox" checked={unmappedOnly} onChange={e=>setUnmappedOnly(e.target.checked)}/>{f('onlyUnmapped')}</label><div className="fm-picker-list" aria-label={f('supplierCategory')}>{filteredCategories.slice(0,150).map(item=>{const mapped=categoryRule(item.source)?.target_code;return <div className="fm-source-item" key={item.source}><button type="button" className="fm-pick-card" aria-pressed={selectedCategory===item.source} draggable onDragStart={e=>startDrag(e,'category',item.source)} onClick={()=>setSelectedCategory(item.source)}><GripVertical size={15}/><span><strong>{item.label}</strong><small>{item.source!==item.label?item.source+' · ':''}{item.count?f('recordCount',{count:item.count}):f('supplierCategory')}</small><em className={mapped?'fm-mapped':''}>{mapped?<><Check size={12}/>{categoryPath(mapped)}</>:f('notMapped')}</em></span></button>{mapped && <button type="button" className="fm-icon-button" aria-label={f('removeCategoryMapping',{name:item.label})} onClick={()=>change({...definition,category_rules:definition.category_rules.filter(rule=>rule.source!==item.source)})}><Trash2 size={15}/></button>}</div>;})}{!filteredCategories.length && <p className="fm-empty">{inspection?f(sourceCategories.length?'noSearchResults':'noDetectedCategories'):f('previewNeedsSource')}</p>}{filteredCategories.length>150 && <p className="fm-hint">{f('narrowSearch',{count:filteredCategories.length})}</p>}</div></div>
            <div className="fm-pane"><div className="fm-pane-heading"><h3>{f('targetCategory')}</h3><span>{categoryTargets.length}</span></div><label>{f('searchTargetCategories')}<input type="search" value={targetFilter} disabled={!shop} onChange={e=>setTargetFilter(e.target.value)}/></label><p className="fm-selection">{selectedCategory?f('selectedSource',{name:sourceCategories.find(item=>item.source===selectedCategory)?.label || selectedCategory}):f('selectSupplierCategory')}</p><div className="fm-picker-list" aria-label={f('targetCategory')}>{categoryTargets.slice(0,150).map(category=><button type="button" className="fm-pick-card fm-drop-target" key={category.code} aria-label={f('connectCategory',{name:categoryPath(category.code)})} aria-pressed={!!selectedCategory && categoryRule(selectedCategory)?.target_code===category.code} onDragOver={e=>e.preventDefault()} onDrop={e=>{const source=dropValue(e,'category');if(source)mapCategory(source,category.code);}} onClick={()=>{if(selectedCategory)mapCategory(selectedCategory,category.code);}}><ArrowRight size={16}/><span><strong>{category.names[i18n.language] || category.names.sk || category.code}</strong><small>{categoryPath(category.code)}</small></span></button>)}{!categoryTargets.length && <p className="fm-empty">{shop?f('noSearchResults'):f('chooseShop')}</p>}{categoryTargets.length>150 && <p className="fm-hint">{f('narrowSearch',{count:categoryTargets.length})}</p>}</div></div></fieldset>
          {shop && <details className="fm-advanced"><summary>{f('manualCategories')}</summary><fieldset disabled={!!busy}><Button variant="secondary" icon={<Plus size={15}/>} onClick={()=>change({...definition,category_rules:[...definition.category_rules,{source:'',target_code:''}]})}>{f('addCategory')}</Button>{definition.category_rules.map((rule,index)=><div className="fm-manual-category" key={index}><input aria-label={f('supplierCategoryRow',{row:index+1})} value={rule.source} onChange={e=>change({...definition,category_rules:definition.category_rules.map((item,i)=>i===index?{...item,source:e.target.value}:item)})}/><CategoryTree categories={leafCategories} value={rule.target_code} language={i18n.language} onChange={target_code=>change({...definition,category_rules:definition.category_rules.map((item,i)=>i===index?{...item,target_code}:item)})}/><button type="button" className="fm-icon-button" aria-label={f('removeRow',{row:index+1})} onClick={()=>change({...definition,category_rules:definition.category_rules.filter((_,i)=>i!==index)})}><Trash2 size={15}/></button></div>)}</fieldset></details>}
        </section>}
        {step==='fields' && <section className="fm-card"><h2>{f('fieldsTitle')}</h2><p>{nativeActive?f('nativeHelp'):f('genericHelp')}</p>{shop && <p className="fm-hint">{f('overlayHelp')}</p>}<p className="fm-hint">{f('fieldConnectHelp')}</p>{!shop && !nativeActive && !!categoryFieldSuggestions.length && <div className="fm-alert"><p>{f('detectedCategoryFields')}</p><Button variant="secondary" disabled={!!busy} onClick={connectCategoryFields}>{f('connectDetectedCategoryFields')}</Button><small>{categoryFieldSuggestions.map(item=>`${targetLabel(item.target)} ← ${item.source}`).join(' · ')}</small></div>}
          <fieldset disabled={!!busy}><div className="fm-workbench fm-field-workbench"><div className="fm-pane"><div className="fm-pane-heading"><h3>{f('sourceField')}</h3><span>{sourceFields.length}</span></div><label>{f('searchFields')}<input type="search" value={sourceFilter} onChange={e=>setSourceFilter(e.target.value)}/></label><div className="fm-picker-list">{visibleFields.slice(0,150).map(field=><button type="button" className="fm-pick-card" key={field.path} aria-label={f('selectField',{name:field.path})} aria-pressed={selectedField===field.path} draggable onDragStart={e=>startDrag(e,'field',field.path)} onClick={()=>setSelectedField(field.path)}><GripVertical size={15}/><span><strong>{fieldLabel(field.path)}</strong><small className="fm-example">{field.examples.map(display).join(' · ') || f('empty')}</small><code>{field.path}</code></span></button>)}{!visibleFields.length && <p className="fm-empty">{f(inspection?'noSearchResults':'previewNeedsSource')}</p>}{visibleFields.length>150 && <p className="fm-hint">{f('narrowSearch',{count:visibleFields.length})}</p>}</div></div>
            <div className="fm-pane fm-destinations"><div className="fm-pane-heading"><h3>{f('targetField')}</h3><span>{definition.bindings.filter(row=>!row.target.startsWith('parameter:')).length}</span></div><div className="fm-new-binding" onDragOver={e=>e.preventDefault()} onDrop={e=>{const source=dropValue(e,'field');if(source && sourceFields.some(field=>field.path===source))appendBinding(effectiveNewFieldTarget,source);}}><label>{f('newFieldTarget')}<select aria-label={f('newFieldTarget')} value={effectiveNewFieldTarget} onChange={e=>setNewFieldTarget(e.target.value)}>{fieldOptions.map(field=><option key={field.key} value={field.key}>{targetLabel(field.key,field.label)}</option>)}</select></label><Button variant="secondary" icon={<Plus size={15}/>} disabled={!selectedField} onClick={()=>appendBinding(effectiveNewFieldTarget,selectedField)}>{f('connectField')}</Button><small>{selectedField?f('selectedSource',{name:fieldLabel(selectedField)}):f('selectSourceField')}</small></div>{renderBindings()}<div className="fm-actions"><Button variant="secondary" icon={<Plus size={15}/>} onClick={()=>appendBinding()}>{f('addField')}</Button><Button variant="secondary" onClick={()=>setStep('parameters')}>{f('steps_parameters')}</Button></div></div></div>
            <label className="fm-checkbox fm-seo"><input type="checkbox" checked={definition.seo_fallback} onChange={e=>change({...definition,seo_fallback:e.target.checked})}/>{f('seoFallback')}</label><p className="fm-hint">{f('seoHelp')}</p>
          </fieldset>
        </section>}
        {step==='parameters' && <section className="fm-card"><h2>{f('parametersTitle')}</h2><p>{f('parameterConnectHelp')}</p><p className="fm-hint">{f('parameterScopeHelp')}</p>{parameterError && <p className="fm-alert">{parameterError}</p>}{!!parameterOptions?.warnings.length && <p className="fm-alert">{f('parameterCatalogWarning')} {parameterOptions.warnings.map(code=>f(`parameterWarnings.${code}`,{defaultValue:code})).join(' · ')}</p>}
          <div className="fm-actions"><Button variant="secondary" disabled={!!busy} icon={<RefreshCw size={15}/>} onClick={()=>execute('parameters',async active=>{const result=await getFeedParameterOptions(supplier,feedKey,shop,undefined,true);if(!active())return;setParameterOptions(result);setParameterError('');})}>{f('refreshParameters')}</Button>{!shop && <span className="fm-hint">{f('chooseShopForParameters')}</span>}</div><fieldset disabled={!!busy}><div className="fm-workbench"><div className="fm-pane"><div className="fm-pane-heading"><h3>{f('feedParameters')}</h3><span>{sourceParameters.length}</span></div><label>{f('searchFeedParameters')}<input type="search" value={parameterFilter} onChange={e=>setParameterFilter(e.target.value)}/></label><div className="fm-picker-list">{sourceParameters.filter(item=>matches(`${item.name} ${item.examples.join(' ')}`,parameterFilter)).slice(0,150).map(item=><button type="button" className="fm-pick-card" key={parameterKey(item)} aria-label={f('selectParameter',{name:item.name})} aria-pressed={selectedParameter===parameterKey(item)} draggable onDragStart={e=>startDrag(e,'parameter',parameterKey(item))} onClick={()=>setSelectedParameter(parameterKey(item))}><GripVertical size={15}/><span><strong>{item.name}</strong><small>{item.examples.join(' · ')}</small><code>{item.source}</code></span></button>)}{!sourceParameters.length && <p className="fm-empty">{f('noFeedParameters')}</p>}</div></div>
            <div className="fm-pane"><div className="fm-pane-heading"><h3>{f('registeredParameters')}</h3><span>{parameterTargets.size}</span></div><label>{f('parameterProfile')}<select aria-label={f('parameterProfile')} value={parameterProfile} onChange={e=>setParameterProfile(e.target.value)}><option value="">{f('allParameters')}</option>{parameterOptions?.category_profiles.map(profile=><option key={profile.id} value={profile.id}>{profile.name}{profile.category_codes.length?` · ${profile.category_codes.map(categoryPath).join(', ')}`:''}</option>)}</select></label><label className="fm-filter-check">{f('searchTargetParameters')}<input type="search" value={parameterTargetFilter} onChange={e=>setParameterTargetFilter(e.target.value)}/></label><p className="fm-selection">{selectedSourceParameter?f('selectedSource',{name:selectedSourceParameter.name}):f('selectFeedParameter')}</p><div className="fm-picker-list">{Array.from(parameterTargets.values()).filter(item=>matches(`${item.name} ${item.origin}`,parameterTargetFilter)).slice(0,150).map(item=><button type="button" className="fm-pick-card fm-drop-target" aria-label={f('connectParameter',{name:item.name})} key={item.name} onDragOver={e=>e.preventDefault()} onDrop={e=>{const source=dropValue(e,'parameter');if(source)mapParameter(source,item.name);}} onClick={()=>{if(selectedParameter)mapParameter(selectedParameter,item.name);}}><ArrowRight size={16}/><span><strong>{item.name}</strong><small>{item.origin}{item.unit?` · ${item.unit}`:''}{item.required?` · ${f('requiredInProfile')}`:''}</small>{!!item.values?.length && <em>{item.values.join(' · ')}</em>}</span></button>)}{!parameterTargets.size && <p className="fm-empty">{f('noRegisteredParameters')}</p>}</div>{parameterOptions?.checked_at && <small className="fm-hint">{f('parametersChecked',{date:new Date(parameterOptions.checked_at).toLocaleString(i18n.language)})}</small>}</div></div>
          <div className="fm-parameter-bindings"><h3>{f('connectedParameters')}</h3>{renderBindings(true)}</div><details className="fm-advanced"><summary>{f('customParameterHelp')}</summary><Button variant="secondary" icon={<Plus size={15}/>} onClick={()=>appendBinding('parameter:')}>{f('addParameter')}</Button></details></fieldset>
        </section>}
        <section ref={previewRef} className="fm-card fm-preview"><div className="fm-section-heading"><div><h2>{f('previewTitle')}</h2><p>{f('previewHelp')}</p></div><div className="fm-actions"><label className="fm-checkbox"><input type="checkbox" checked={automaticPreview} onChange={e => setAutomaticPreview(e.target.checked)} />{f('autoPreview')}</label><Button variant="secondary" icon={<Eye size={15} />} disabled={!!busy || !inspection || previewBusy} loading={previewBusy} onClick={() => runPreview()}>{f('preview')}</Button></div></div>
          {previewError && <p className="fm-alert fm-error" role="alert">{previewError}</p>}
          {!preview && <p className="fm-empty">{inspection ? f('previewStale') : f('previewNeedsSource')}</p>}
          {preview && <>{preview.errors.length > 0 && <div className="fm-alert fm-error" role="alert">{preview.errors.map((item,index) => <p key={index}>{f('previewRowError', {row: item.row, message:item.message})}</p>)}</div>}<div className="fm-table-scroll"><table><thead><tr><th>{targetLabel('name')}</th><th>{targetLabel('eans')}</th><th>{targetLabel('brand')}</th><th>{targetLabel('prices.purchase_net')}</th><th>{targetLabel('prices.retail_gross')}</th><th>{f('categoryAndSeo')}</th><th>{f('details')}</th></tr></thead><tbody>{preview.items.map((item,index) => <tr key={item.code || index}><td><strong>{item.name}</strong><small>{item.code}</small></td><td>{item.eans?.join(', ')}</td><td>{item.brand}</td><td>{item.prices?.purchase_net ?? '—'} {item.prices?.currency}</td><td>{item.prices?.retail_gross ?? '—'} {item.prices?.currency}</td><td>{previewCategory(item)}<small>{display((item as unknown as Record<string,unknown>).seo_title)}</small></td><td><details><summary>{f('showValues')}</summary><dl className="fm-product-values">{Object.entries(item).filter(([key]) => !['source_xml', 'source_fields', 'manufacturer_description'].includes(key)).map(([key,value]) => <React.Fragment key={key}><dt>{targetLabel(key)}</dt><dd>{display(value) || f('empty')}</dd></React.Fragment>)}</dl></details></td></tr>)}</tbody></table></div></>}
        </section>
        <footer className="fm-save-bar"><div><strong>{dirty ? f('unsaved') : f('savedState')}</strong><small>{f('saveHelp')}</small></div><Button disabled={!!busy || (!dirty && config.configured)} loading={busy === 'save'} icon={<Save size={16} />} onClick={() => execute('save', async active => { const saved = await saveFeedMapping(supplier, feedKey, shop, config.revision, definition); if(!active())return; setConfig(saved); setDefinition(normalizeDefinition(saved.definition)); setNotice(f('saved')); })}>{f('save')}</Button></footer>
        {!shop && config.configured && <details className="fm-card fm-apply"><summary>{f('applyTitle')}</summary><p>{f('applyHelp')}</p><Button variant="secondary" disabled={!!busy || dirty || !preview || !!preview.errors.length} loading={busy === 'apply'} onClick={() => execute('apply', async active => { await applySavedFeedMapping(supplier, feedKey, config.revision, sampleId); if(!active())return; setNotice(f('applied')); })}>{f('apply')}</Button></details>}
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
