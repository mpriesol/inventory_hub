import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import type { AtomerAvailabilityChanges, AtomerAvailabilityDetail, AtomerAvailabilityQuery, AtomerAvailabilityRow,
  AtomerAvailabilitySelection, AtomerAvailabilitySort } from '../api/atomerAvailability';
import { Button } from '../components/ui/Button.new';
import { ProductThumb } from '../components/product/ProductDisplay';
import { StockCardLink } from '../components/product/StockCardLink';

export const atomerRowEditable = (row: AtomerAvailabilityRow) => row.editable ??
  (!['blocked', 'unmatched'].includes(row.status) && !row.reasons.length && row.current_visibility !== null);

/** Page slices retain their family context; selection excludes implicit context rows. */
function families(rows: AtomerAvailabilityRow[]) {
  const result = new Map<string, { parent?: AtomerAvailabilityRow; name: string; image?: string | null; children: AtomerAvailabilityRow[] }>();
  for (const row of rows) {
    const key = row.parent_code || row.code;
    let family = result.get(key);
    if (!family) { family = { name: row.parent_code ? row.parent_name || row.parent_code : row.name, image: row.parent_code ? row.parent_image : row.image, children: [] }; result.set(key, family); }
    if (row.kind === 'parent') { family.parent = row; family.name = row.name; family.image = row.image; }
    else family.children.push(row);
  }
  return [...result.entries()];
}
interface Props {
  run: AtomerAvailabilityDetail; query: AtomerAvailabilityQuery; canEdit: boolean; busy: boolean; canRead: boolean;
  load: (query: AtomerAvailabilityQuery, offset?: number) => void;
  edit: (selection: AtomerAvailabilitySelection, changes: AtomerAvailabilityChanges) => void;
}
export function AtomerAvailabilityEditor({ run, query, canEdit, busy, canRead, load, edit }: Props) {
  const { t } = useTranslation();
  const [search, setSearch] = useState(query.search || '');
  const [selected, setSelected] = useState<Set<string>>(new Set()), [excluded, setExcluded] = useState<Set<string>>(new Set());
  const [allFiltered, setAllFiltered] = useState(false);
  const [bulkVisibility, setBulkVisibility] = useState(''), [bulkInclude, setBulkInclude] = useState(''), [bulkAvailability, setBulkAvailability] = useState('');
  const [bulkPersistent, setBulkPersistent] = useState(false);
  const { sort, direction, ...filter } = query, filterKey = JSON.stringify(filter);
  const clearSelection = () => { setSelected(new Set()); setExcluded(new Set()); setAllFiltered(false); };
  // A confirmed edit can change filter membership, including rows excluded on other pages.
  // Finish the previous selection instead of subtracting stale exceptions from new totals.
  useEffect(clearSelection, [run.id, run.revision, filterKey]);
  useEffect(() => setSearch(query.search || ''), [query.search]);
  const count = allFiltered ? Math.max(0, (run.selectable_total ?? run.total) - excluded.size) : selected.size;
  const pageRows = run.rows.filter(row => atomerRowEditable(row) && row.matches_filter !== false);
  const isSelected = (row: AtomerAvailabilityRow) => allFiltered ? row.matches_filter !== false && !excluded.has(row.code) : selected.has(row.code);
  const patchQuery = (patch: Partial<AtomerAvailabilityQuery>) => load({ ...query, ...patch });
  function toggle(code: string, checked: boolean) {
    const next = new Set(allFiltered ? excluded : selected);
    if (allFiltered ? !checked : checked) next.add(code); else next.delete(code);
    (allFiltered ? setExcluded : setSelected)(next);
  }
  function selectPage(checked: boolean) {
    const next = new Set(allFiltered ? excluded : selected);
    for (const row of pageRows) { if (allFiltered ? !checked : checked) next.add(row.code); else next.delete(row.code); }
    (allFiltered ? setExcluded : setSelected)(next);
  }
  function bulkEdit() {
    const changes: AtomerAvailabilityChanges = {};
    if (bulkInclude) changes.include_in_export = bulkInclude === 'true';
    if (bulkVisibility) changes.visibility = bulkVisibility === 'true';
    if (bulkAvailability) changes.availability = bulkAvailability;
    if (bulkVisibility || bulkAvailability) changes.apply_to_every_export = bulkPersistent;
    if (!count || !Object.keys(changes).length) return;
    edit(allFiltered ? { all_filtered: true, filter, excluded_codes: [...excluded].sort() } : { codes: [...selected].sort() }, changes);
  }
  const boolFilter = (key: 'include_in_export' | 'manual_override', label: string) => <label>{t(`atomerAvailability.${label}`)}<select
    value={query[key] === undefined || query[key] === null ? '' : String(query[key])} disabled={!canRead} data-testid={`atomer-filter-${key}`}
    onChange={event => patchQuery({ [key]: event.target.value === '' ? undefined : event.target.value === 'true' })}>
    <option value="">{t('atomerAvailability.anyValue')}</option><option value="true">{t('atomerAvailability.yes')}</option><option value="false">{t('atomerAvailability.no')}</option>
  </select></label>;
  const rowView = (row: AtomerAvailabilityRow) => {
    const editable = canEdit && atomerRowEditable(row), availability = row.proposed_availability;
    const choices = run.availability_options || [], provenance = row.availability_override;
    const cardOverride = provenance && (provenance.label !== undefined || provenance.visible !== undefined);
    const warnings = [...new Set([...row.reasons, ...(row.warnings || [])])];
    return <tr key={row.code} className={row.kind === 'parent' ? 'atomer-family-row' : row.kind === 'variant' ? 'atomer-variant' : ''} data-testid={`atomer-row-${row.code}`}>
      <td className="atomer-select-cell"><input type="checkbox" checked={atomerRowEditable(row) && isSelected(row)} disabled={!editable || row.matches_filter === false}
        data-testid={`atomer-select-${row.code}`} aria-label={t('atomerAvailability.selectFor', { code: row.code })} onChange={event => toggle(row.code, event.target.checked)} /></td>
      <td><div className="atomer-product"><ProductThumb key={row.image || row.code} url={row.image} name={row.name} size={48} /><div className="atomer-product-text">
        <strong>{row.product_id ? <StockCardLink productId={row.product_id} sku={row.sku} warehouseCode={run.warehouse_code}>{row.name}</StockCardLink> : row.name}</strong>
        <code>{row.code}</code>{row.sku && row.sku !== row.code && <small>Hub: {row.sku}</small>}
        {row.manual_override && <span className="atomer-status" title={[provenance?.actor, provenance?.updated_at].filter(Boolean).join(' · ')}>{t(cardOverride ? 'atomerAvailability.manualOverride' : 'atomerAvailability.edited')}</span>}
      </div></div></td>
      <td>{row.qty_available === null ? t('atomerAvailability.unknown') : row.qty_available}</td>
      <td><label className="atomer-visibility"><input type="checkbox" checked={row.include_in_export !== false} disabled={!editable}
        data-testid={`atomer-include-${row.code}`} aria-label={t('atomerAvailability.includeFor', { code: row.code })}
        onChange={event => edit({ codes: [row.code] }, { include_in_export: event.target.checked })} />{t('atomerAvailability.includeExport')}</label></td>
      <td><span className="atomer-before">{t('atomerAvailability.current')}: {row.current_visibility === null ? t('atomerAvailability.unknown') : t(`atomerAvailability.${row.current_visibility ? 'visible' : 'hidden'}`)}</span>
        <label className="atomer-visibility"><input type="checkbox" data-testid={`atomer-visible-${row.code}`} checked={row.proposed_visibility === true} disabled={!editable || row.proposed_visibility === null}
          aria-label={t('atomerAvailability.visibilityFor', { code: row.code })} onChange={event => edit({ codes: [row.code] }, { visibility: event.target.checked })} />
          {row.proposed_visibility === null ? t('atomerAvailability.unknown') : t(`atomerAvailability.${row.proposed_visibility ? 'visible' : 'hidden'}`)}</label></td>
      <td><span className="atomer-before">{t('atomerAvailability.current')}: {row.current_availability || t('atomerAvailability.unknown')}</span>
        <select data-testid={`atomer-availability-${row.code}`} aria-label={t('atomerAvailability.availabilityFor', { code: row.code })}
          value={availability || ''} disabled={!editable} onChange={event => edit({ codes: [row.code] }, { availability: event.target.value })}>
          {!choices.includes(availability) && <option value={availability || ''} disabled>{availability || t('atomerAvailability.unknown')}</option>}
          {choices.map(value => <option key={value} value={value}>{value}</option>)}
        </select>
        {row.product_id && row.kind !== 'parent' && <label className="atomer-check-label atomer-persist"><input type="checkbox" data-testid={`atomer-persist-${row.code}`}
          checked={!!row.apply_to_every_export} disabled={!editable} onChange={event => edit({ codes: [row.code] }, { apply_to_every_export: event.target.checked })} />{t('atomerAvailability.applyEveryExport')}</label>}
      </td>
      <td><span className="atomer-status">{t(`atomerAvailability.rowStatus.${row.status}`)}</span>
        {!!warnings.length && <div className="atomer-row-warning">{warnings.map(reason => <span key={reason}>{t(`atomerAvailability.errors.${reason}`, { defaultValue: t('atomerAvailability.requestFailed') })}</span>)}</div>}
        {row.supplier_reason && <small>{t(`productEditor.supplierStatus.${row.supplier_reason === 'supplier_quantity_unknown' ? 'unknown_quantity' : row.supplier_status || 'unknown'}`, { defaultValue: t('atomerAvailability.supplierUncertain') })}</small>}
      </td>
    </tr>;
  };
  return <>
    <form className="atomer-fields" onSubmit={event => { event.preventDefault(); patchQuery({ search: search.trim() }); }}>
      <label className="atomer-search">{t('atomerAvailability.search')}<input type="search" maxLength={255} value={search} disabled={!canRead} data-testid="atomer-search" onChange={event => setSearch(event.target.value)} /></label>
      <Button type="submit" variant="secondary" disabled={!canRead} data-testid="atomer-search-submit">{t('atomerAvailability.searchAction')}</Button>
      <label>{t('atomerAvailability.viewLabel')}<select value={query.view} disabled={!canRead} data-testid="atomer-view" onChange={event => patchQuery({ view: event.target.value as AtomerAvailabilityQuery['view'] })}>
        {(['changes', 'all', 'issues'] as const).map(value => <option key={value} value={value}>{t(`atomerAvailability.views.${value}`)}</option>)}
      </select></label>
      <label>{t('atomerAvailability.sortBy')}<select value={query.sort || 'code'} disabled={!canRead} data-testid="atomer-sort" onChange={event => patchQuery({ sort: event.target.value as AtomerAvailabilitySort })}>
        {(['code', 'name', 'sku', 'ean', 'qty_available', 'current_visibility', 'proposed_visibility', 'current_availability', 'proposed_availability', 'status', 'include_in_export', 'manual_override'] as const).map(value => <option key={value} value={value}>{t(`atomerAvailability.sorts.${value}`)}</option>)}
      </select></label>
      <label>{t('atomerAvailability.sortDirection')}<select value={query.direction || 'asc'} disabled={!canRead} data-testid="atomer-direction" onChange={event => patchQuery({ direction: event.target.value as 'asc' | 'desc' })}><option value="asc">{t('atomerAvailability.ascending')}</option><option value="desc">{t('atomerAvailability.descending')}</option></select></label>
    </form>
    <details className="atomer-filters"><summary>{t('atomerAvailability.filters')}</summary><div className="atomer-fields">
      <label>{t('atomerAvailability.rowKind')}<select value={query.kind || 'all'} disabled={!canRead} data-testid="atomer-filter-kind" onChange={event => patchQuery({ kind: event.target.value as AtomerAvailabilityQuery['kind'] })}>
        {(['all', 'parent', 'variant', 'standalone'] as const).map(value => <option key={value} value={value}>{t(`atomerAvailability.kinds.${value}`)}</option>)}
      </select></label>
      <label>{t('atomerAvailability.result')}<select value={query.status || 'all'} disabled={!canRead} data-testid="atomer-filter-status" onChange={event => patchQuery({ status: event.target.value as AtomerAvailabilityQuery['status'] })}><option value="all">{t('atomerAvailability.anyValue')}</option>
        {(['changed', 'unchanged', 'context', 'blocked', 'unmatched'] as const).map(value => <option key={value} value={value}>{t(`atomerAvailability.rowStatus.${value}`)}</option>)}
      </select></label>
      {(['visibility', 'current_visibility'] as const).map(key => <label key={key}>{t(`atomerAvailability.sorts.${key === 'visibility' ? 'proposed_visibility' : key}`)}<select value={query[key] || 'all'} disabled={!canRead} data-testid={`atomer-filter-${key.replace('_', '-')}`} onChange={event => patchQuery({ [key]: event.target.value })}><option value="all">{t('atomerAvailability.anyValue')}</option>{['visible', 'hidden', 'unknown'].map(value => <option key={value} value={value}>{t(`atomerAvailability.${value}`)}</option>)}</select></label>)}
      {(['availability', 'current_availability'] as const).map(key => <label key={key}>{t(`atomerAvailability.sorts.${key === 'availability' ? 'proposed_availability' : key}`)}<select value={query[key] || ''} disabled={!canRead} data-testid={`atomer-filter-${key.replace('_', '-')}`} onChange={event => patchQuery({ [key]: event.target.value || undefined })}><option value="">{t('atomerAvailability.anyValue')}</option>{(run.filter_options?.availability || run.availability_options || []).map(value => <option key={value} value={value}>{value}</option>)}</select></label>)}
      {boolFilter('include_in_export', 'includeExport')}{boolFilter('manual_override', 'manualChanges')}
      <Button variant="secondary" disabled={!canRead} data-testid="atomer-reset-filters" onClick={() => { setSearch(''); load({ view: 'changes', sort: 'code', direction: 'asc' }); }}>{t('atomerAvailability.resetFilters')}</Button>
    </div></details>
    {canEdit && <div className="atomer-bulk" data-testid="atomer-bulk">
      <div className="atomer-actions"><strong>{t('atomerAvailability.selectedCount', { count })}</strong>
        <Button size="sm" variant="secondary" disabled={busy || !run.selectable_total && !pageRows.length} data-testid="atomer-select-all" onClick={() => { setAllFiltered(true); setSelected(new Set()); setExcluded(new Set()); }}>{t('atomerAvailability.selectAllFiltered', { count: run.selectable_total ?? run.total })}</Button>
        <Button size="sm" variant="secondary" disabled={!count} onClick={clearSelection} data-testid="atomer-clear-selection">{t('atomerAvailability.clearSelection')}</Button>
      </div>{allFiltered && <p className="atomer-muted">{t('atomerAvailability.allPagesSelected')}</p>}
      <div className="atomer-fields">
        <label>{t('atomerAvailability.includeExport')}<select value={bulkInclude} data-testid="atomer-bulk-include" onChange={event => setBulkInclude(event.target.value)}><option value="">{t('atomerAvailability.leaveUnchanged')}</option><option value="true">{t('atomerAvailability.includeExport')}</option><option value="false">{t('atomerAvailability.excludeExport')}</option></select></label>
        <label>{t('atomerAvailability.visibility')}<select value={bulkVisibility} data-testid="atomer-bulk-visibility" onChange={event => setBulkVisibility(event.target.value)}><option value="">{t('atomerAvailability.leaveUnchanged')}</option><option value="true">{t('atomerAvailability.visible')}</option><option value="false">{t('atomerAvailability.hidden')}</option></select></label>
        <label>{t('atomerAvailability.availability')}<select value={bulkAvailability} data-testid="atomer-bulk-availability" onChange={event => setBulkAvailability(event.target.value)}><option value="">{t('atomerAvailability.leaveUnchanged')}</option>{run.availability_options?.map(value => <option key={value} value={value}>{value}</option>)}</select></label>
        <label className="atomer-check-label"><input type="checkbox" checked={bulkPersistent} data-testid="atomer-bulk-persist" disabled={!bulkVisibility && !bulkAvailability} onChange={event => setBulkPersistent(event.target.checked)} />{t('atomerAvailability.applyEveryExport')}</label>
        <Button variant="secondary" disabled={busy || !count || !bulkInclude && !bulkVisibility && !bulkAvailability} data-testid="atomer-bulk-apply" onClick={bulkEdit}>{t('atomerAvailability.applySelected', { count })}</Button>
      </div><p className="atomer-muted">{t('atomerAvailability.persistenceHelp')}</p>
      <Link to="/settings/availability#dictionary">{t('atomerAvailability.manageAvailabilities')}</Link>
    </div>}
    <div className="atomer-table-scroll"><table className="atomer-table"><thead><tr>
      <th><input type="checkbox" data-testid="atomer-select-page" ref={element => { if (element) element.indeterminate = pageRows.some(isSelected) && !pageRows.every(isSelected); }} checked={!!pageRows.length && pageRows.every(isSelected)} disabled={!canEdit || !pageRows.length} aria-label={t('atomerAvailability.selectPage')} onChange={event => selectPage(event.target.checked)} /></th>
      {['product', 'availableQty', 'includeExport', 'visibility', 'availability', 'result'].map(key => <th key={key}>{t(`atomerAvailability.${key}`)}</th>)}</tr></thead>
      {families(run.rows || []).map(([key, family]) => <tbody key={key}>
        {family.parent ? rowView(family.parent) : family.children[0]?.parent_code && <tr className="atomer-family-row"><td /><td colSpan={6}><div className="atomer-product"><ProductThumb url={family.image} name={family.name} size={48} /><div className="atomer-product-text"><strong>{family.name}</strong><code>{key}</code></div></div></td></tr>}
        {family.children.map(rowView)}
      </tbody>)}
    </table></div>
    {!run.rows?.length && <p>{t('atomerAvailability.noRows')}</p>}
    <div className="atomer-pagination"><Button variant="secondary" size="sm" disabled={busy || run.offset === 0} onClick={() => load(query, Math.max(0, run.offset - 100))} data-testid="atomer-previous">{t('atomerAvailability.previous')}</Button>
      <span>{t('atomerAvailability.page', { from: run.total ? run.offset + 1 : 0, to: Math.min(run.offset + (run.rows?.length || 0), run.total), total: run.total })}</span>
      <Button variant="secondary" size="sm" disabled={busy || run.offset + (run.rows?.length || 0) >= run.total} onClick={() => load(query, run.offset + 100)} data-testid="atomer-next">{t('atomerAvailability.next')}</Button></div>
  </>;
}
