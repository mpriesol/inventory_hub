import React, { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { ExternalLink } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { bikeStates, progressStates, readProgress, refreshProgress, ProgressPage, ProgressRow, ProgressFamily } from '../api/atomerProgress';
import './OpeningStockPage.css';
import './AtomerMigrationProgress.css';

const pageSize = 20;
export function AtomerMigrationProgress() {
  const { t, i18n } = useTranslation();
  const m = (key: string) => t('atomerProgress.' + key);
  const num = (n: number) => n.toLocaleString(i18n.language);
  const date = (s: string) => new Date(s).toLocaleString(i18n.language, {timeZone:'Europe/Bratislava', dateStyle:'short',timeStyle:'short'});
  const [page,setPage] = useState<ProgressPage | null>(null);
  const [kind,setKind] = useState(''), [state,setState] = useState(''), [bike,setBike] = useState('');
  const [card,setCard] = useState('');
  const [expanded,setExpanded] = useState<Set<string>>(new Set());
  const [query,setQuery] = useState(''), [offset,setOffset] = useState(0), [revision,setRevision] = useState(0);
  const [pending,setPending] = useState(false), [reading,setReading] = useState(false), [error,setError] = useState('');
  const [file,setFile] = useState<File | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const auto = useRef<number | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams({offset:String(offset),limit:String(pageSize)});
    for (const [k,v] of Object.entries({kind,state,bike,card,search:query.trim()})) if (v) params.set(k,v);
    setReading(true);
    const timer = window.setTimeout(() => {
      void readProgress(params,controller.signal).then(p => {
        if (!controller.signal.aborted) { setPage(p); setError(''); }
      }).catch(e => { if (!controller.signal.aborted) setError(e?.code || 'failed'); })
        .finally(() => { if (!controller.signal.aborted) setReading(false); });
    },180);
    return () => { window.clearTimeout(timer); controller.abort(); };
  },[kind,state,bike,card,query,offset,revision]);
  const run = page?.run;
  async function update(selected: File | null, staleOnly = false) {
    if (pending || !page) return;
    setPending(true); setError('');
    try {
      await refreshProgress(page.run?.id || 0, selected, staleOnly);
      if (selected) { setFile(null); if (input.current) input.current.value = ''; }
      setOffset(0); setRevision(n => n+1);
    } catch (e: any) {
      if (e?.code === 'run_changed') setRevision(n=>n+1);
      else setError(e?.code || 'failed');
    } finally { setPending(false); }
  }
  useEffect(() => {
    if (!run || auto.current === run.id || Date.now() - Date.parse(run.checked_at) < 86400000) return;
    auto.current = run.id;
    void update(null,true);
  },[run?.id]);
  function filter(set: (x:string)=>void, value: string) { setOffset(0); setExpanded(new Set()); set(value); }
  const anchors = (web: string | null | undefined, admin: string | null | undefined) =>
    <div className="progress-shop-links">{web && <a href={web} target="_blank" rel="noopener noreferrer">{m('web')} <ExternalLink size={12} aria-hidden="true" /></a>}
      {admin && <a href={admin} target="_blank" rel="noopener noreferrer">{m('admin')} <ExternalLink size={12} aria-hidden="true" /></a>}</div>;
  function itemRow(r: ProgressRow, child=false) {
    const shopTitle = (code: string|null, product: string|null, variant: string|null) =>
      [code, product && 'ID '+product, variant && 'ID '+variant].filter(Boolean).join(' · ');
    const bikeRecord = r.bike;
    return <tr className={child?'progress-child':'progress-single'} key={r.code}>
      <td><strong>{child ? r.variant_label || r.name : r.name || r.code}</strong>
        <small>{r.code}{r.ean && r.ean !== r.code && ' · EAN '+r.ean}</small></td>
      <td>{r.card_state==='exists' && r.hub_sku ? <Link className="progress-card-link" to={'/products/'+encodeURIComponent(r.hub_sku)}>
        {m('yes')} · {r.hub_sku}</Link> : <span className="progress-muted">{m('cardStates.'+r.card_state)}</span>}
        {r.hub_id && <small>ID {r.hub_id}</small>}{!!r.candidates.length && <small>{r.candidates.join(', ')}</small>}</td>
      <td title={shopTitle(r.target_code,r.product_id,r.variant_id)}>{r.target_code || '—'}{anchors(r.shop_url,r.shop_admin_url)}</td>
      <td title={bikeRecord ? shopTitle(bikeRecord.code,bikeRecord.product_id,bikeRecord.variant_id) : undefined}>
        {bikeRecord?.code || m('bikeStates.'+r.bike_state)}{anchors(bikeRecord?.shop_url,bikeRecord?.shop_admin_url)}
        {bikeRecord && r.bike_state==='review' && <small className="progress-issue">{m(bikeRecord.reason?'bikeReasons.'+bikeRecord.reason:'bikeStates.review')}</small>}</td>
      <td><span className={r.state==='linked'?'progress-good':'progress-issue'}>{m('states.'+r.state)}</span>
        {r.reasons.map(k=><small key={k}>{m('reasons.'+k)}</small>)}</td>
    </tr>;
  }
  function familyRows(g: ProgressFamily) {
    if (g.items.length===1 && g.items[0].kind==='standalone') return itemRow(g.items[0]);
    const open = expanded.has(g.key), n = g.items.length;
    function toggle() { setExpanded(prev=>{const next=new Set(prev); if (open) next.delete(g.key); else next.add(g.key); return next;}); }
    const haveCard = g.items.filter(r=>r.card_state==='exists').length;
    const haveX = g.items.filter(r=>r.state==='linked').length, haveB = g.items.filter(r=>r.bike_state==='linked').length;
    const problems = g.items.filter(r=>r.state!=='linked' || r.bike_state==='review').length;
    function groupLinks(isBike:boolean) {
      const rows = g.items.filter(r=> isBike ? r.bike_state==='linked' : !!r.target_code);
      const ids = new Set(rows.map(r=>isBike?r.bike?.product_id:r.product_id));
      if (ids.size!==1) return null;
      const web = new Set(rows.map(r=>isBike?r.bike?.shop_url:r.shop_url).filter(Boolean));
      const admin = new Set(rows.map(r=>isBike?r.bike?.shop_admin_url:r.shop_admin_url).filter(Boolean));
      return anchors(web.size===1?[...web][0]:null, admin.size===1?[...admin][0]:null);
    }
    return <React.Fragment key={g.key}><tr className="progress-family">
      <td><button className="progress-toggle" type="button" aria-expanded={open} onClick={toggle}>
        <span aria-hidden="true" className={open?'is-open':''}>▸</span><span><strong>{g.name}</strong>
        <small>{g.items[0].parent} · {m('variantsCount')} {num(n)}</small></span></button></td>
      <td>{num(haveCard)} / {num(n)}</td><td>{num(haveX)} / {num(n)}{groupLinks(false)}</td>
      <td>{num(haveB)} / {num(n)}{groupLinks(true)}</td>
      <td>{problems ? <span className="progress-issue">{m('toResolve')} {num(problems)}</span>
        : <span className="progress-good">{m('verified')}</span>}</td>
      </tr>{open && g.items.map(r=>itemRow(r,true))}</React.Fragment>;
  }
  const upload = <div className="progress-upload">
    <label>{m('file')} <input ref={input} type="file" accept=".xml,application/xml,text/xml" onChange={e=>setFile(e.target.files?.[0]||null)} /></label>
    <button type="button" disabled={!file || pending || !page} onClick={()=>void update(file)}>{m('useFile')}</button>
  </div>;
  return <main className="opening-stock atomer-progress">
    <Link className="opening-stock-back" to="/shops">{m('back')}</Link>
    <header><h1>{m('title')}</h1><p>{m('scope')}</p></header>
    {error && <p role="alert" className="progress-warning">{t('atomerProgress.errors.'+error,{defaultValue:m('errors.failed')})}</p>}
    {run && <>
      <div className="progress-counts">
        {[[m('kinds.variant'),run.totals.kinds.variant||0],[m('kinds.standalone'),run.totals.kinds.standalone||0],
          [m('states.linked'),run.totals.states.linked||0],[m('bikeLinked'),run.totals.bike.linked||0],
          [m('states.review'),run.totals.states.review||0]].map(([label,n])=><div key={label}><span>{label}</span><strong>{num(Number(n))}</strong></div>)}
      </div>
      <p className="progress-meta">{m('asOf')} {date(run.checked_at)} · {m('source')} {date(run.imported_at)} · {num(run.totals.items)} {m('codes')}</p>
    </>}
    <section className={run?'progress-tools progress-compact-tools':'opening-stock-panel progress-tools'}>
      {run ? <><button type="button" disabled={pending || reading} onClick={()=>void update(null)}>{m('refresh')}</button>
        <details><summary>{m('replaceSource')}</summary>{upload}</details></> : upload}
      {pending && <span role="status">{m('refreshing')}</span>}
    </section>
    {page && !run && !pending && <p>{m('emptySource')}</p>}
    {run && <>
      <div className="progress-filters">
        <label>{m('search')}<input type="search" value={query} placeholder={m('searchHint')} onChange={e=>filter(setQuery,e.target.value)}/></label>
        <label>{m('kind')}<select value={kind} onChange={e=>filter(setKind,e.target.value)}><option value="">{m('all')}</option>
          {['variant','standalone'].map(k=><option value={k} key={k}>{m('kinds.'+k)}</option>)}</select></label>
        <label>{m('card')}<select value={card} onChange={e=>filter(setCard,e.target.value)}><option value="">{m('all')}</option>
          {['exists','missing','review'].map(k=><option value={k} key={k}>{m('cardStates.'+k)}</option>)}</select></label>
        <label>xTrek<select value={state} onChange={e=>filter(setState,e.target.value)}><option value="">{m('all')}</option>
          {progressStates.map(k=><option value={k} key={k}>{m('states.'+k)}</option>)}</select></label>
        <label>BIKETREK<select value={bike} onChange={e=>filter(setBike,e.target.value)}><option value="">{m('all')}</option>
          {bikeStates.map(k=><option value={k} key={k}>{m('bikeStates.'+k)}</option>)}</select></label>
      </div>
      <div className="progress-grid" aria-busy={reading}><table><thead><tr>
        {[m('item'),m('card'),'xTrek','BIKETREK',m('status')].map(h=><th key={h}>{h}</th>)}
      </tr></thead><tbody>{page?.groups.map(familyRows)}</tbody></table></div>
      {page && <div className="progress-pager"><span>{page.families ? m('productPages')+' '+num(offset+1)+'–'+num(Math.min(offset+pageSize,page.families))+' / '+num(page.families)+' · '+num(page.total)+' '+m('codes') : m('empty')}</span>
        <button disabled={reading || offset===0} onClick={()=>setOffset(Math.max(0,offset-pageSize))}>{m('previous')}</button>
        <button disabled={reading || offset+pageSize>=page.families} onClick={()=>setOffset(offset+pageSize)}>{m('next')}</button></div>}
    </>}
  </main>;
}
