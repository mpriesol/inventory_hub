import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { ImportColumn, importValueText, parseImportValue, categoryPath, FAMILY_IMPORT_FIELDS } from '../../pages/productImportGrid';
import { ImportValue, ProductImportDraft, ProductImportRow } from '../../api/productImport';
import { CategoryTree } from './CategoryTree';
import { Button } from '../ui/Button.new';

export function ImportCellEditor({ column, row, value, draft, onApply, onClose, onDirtyChange }: {
  column: ImportColumn; row: ProductImportRow; value: ImportValue; draft: ProductImportDraft;
  onApply: (value: ImportValue) => void; onClose: () => void; onDirtyChange?: (dirty: boolean) => void;
}) {
  const { t, i18n } = useTranslation();
  const c = (key: string) => t(`productImport.${key}`);
  const [text, setText] = useState(importValueText(value));
  const [error, setError] = useState('');
  const panel = useRef<HTMLDivElement>(null);
  const original = importValueText(value);
  useEffect(() => { onDirtyChange?.(text !== original); }, [text, original, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);
  const close = () => { if (text === original || window.confirm(c('discardCell'))) onClose(); };
  const closeRef = useRef(close); closeRef.current = close;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow; document.body.style.overflow = 'hidden';
    panel.current?.querySelector<HTMLElement>('textarea,input,summary,button')?.focus();
    function key(event: KeyboardEvent) {
      if (event.key === 'Escape') { event.preventDefault(); closeRef.current(); }
      if (event.key !== 'Tab') return;
      const elements = Array.from(panel.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),summary') || []).filter(element => element.getClientRects().length);
      const first = elements[0], last = elements[elements.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
    document.addEventListener('keydown', key);
    return () => { document.body.style.overflow = overflow; document.removeEventListener('keydown', key); previous?.focus(); };
  }, []);
  const categoryOptions = draft.categories.map(category => ({ ...category, assignable: category.assignable !== false && category.active !== false && !draft.categories.some(child => child.parent_code === category.code && child.assignable !== false) }));
  function apply() {
    try {
      if (column.type === 'category' && text && !categoryOptions.some(category => category.code === text && category.assignable)) throw new Error('invalidCategory');
      onApply(parseImportValue(column, text));
    } catch (error) { setError((error as Error).message); }
  }
  return createPortal(<div className="product-import-editor-backdrop"><div className="product-import product-import-cell-editor" role="dialog" aria-modal="true" aria-labelledby="import-cell-title" ref={panel}>
    <header><div><small>{row.values.code}</small><h2 id="import-cell-title">{c(`fields.${column.key}`)}</h2></div><Button variant="ghost" onClick={close} aria-label={c('close')}>×</Button></header>
    {row.is_variant && FAMILY_IMPORT_FIELDS.has(column.key) && <p className="import-notice">{c('familyField')}</p>}
    <div className="import-cell-editor-body">
      {column.type === 'category' ? <><CategoryTree categories={categoryOptions} value={text} language={i18n.language} onChange={setText} /><p>{categoryPath(draft.categories, text, i18n.language)}</p><p className="import-muted">{c('ancestorsHelp')}</p></> : <>
        {(column.type === 'pairs' || column.type === 'metadata') && <p className="import-muted">{c('pairsHelp')}</p>}
        {column.type === 'list' && <p className="import-muted">{c(column.key === 'eans' ? 'eansHelp' : 'imagesHelp')}</p>}
        {column.type === 'long' || column.type === 'pairs' || column.type === 'metadata' || column.type === 'list' ? <textarea aria-label={c(`fields.${column.key}`)} className={column.key === 'description_html' ? 'import-html-editor' : ''} rows={column.type === 'long' ? 14 : 9} value={text} onChange={event => setText(event.target.value)} /> : <input aria-label={c(`fields.${column.key}`)} value={text} onChange={event => setText(event.target.value)} />}
      </>}
      {error && <p className="import-error" role="alert">{c(`errors.${error}`)}</p>}
    </div>
    <footer><Button variant="secondary" onClick={close}>{c('cancel')}</Button><Button onClick={apply}>{c('applyCell')}</Button></footer>
  </div></div>, document.body);
}
