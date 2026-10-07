import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { AiJob } from '../../api/aiContent';
import { AiJobDetail } from './AiJobDetail';

export function AiJobModal({job, publishedVersion, onChange, onClose}: {
  job: AiJob; publishedVersion?: number; onChange: (job: AiJob) => void; onClose: () => void;
}) {
  const { t } = useTranslation();
  const panel = useRef<HTMLDivElement>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirmClose, setConfirmClose] = useState(false);
  function close() { if (busy) return; if (dirty) setConfirmClose(true); else onClose(); }
  const closeRef = useRef(close); closeRef.current = close;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow; document.body.style.overflow = 'hidden';
    panel.current?.querySelector<HTMLButtonElement>('button')?.focus();
    function key(event: KeyboardEvent) {
      // A nested import dialog owns its own Escape handling.
      if (event.key === 'Escape' && !panel.current?.querySelector('.fixed.inset-0')) { event.preventDefault(); closeRef.current(); }
      if (event.key !== 'Tab') return;
      const elements = [...(panel.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], summary') || [])].filter(e => e.getClientRects().length);
      const first = elements[0], last = elements[elements.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
    document.addEventListener('keydown', key);
    return () => { document.body.style.overflow = overflow; document.removeEventListener('keydown', key); previous?.focus(); };
  }, []);
  return createPortal(<div className="ai-job-backdrop"><div ref={panel} role="dialog" aria-modal="true" aria-labelledby="ai-job-modal-title" className="ai-content ai-job-modal">
    <div className="ai-job-modal-header"><h2 id="ai-job-modal-title">{t('ai.jobDetailTitle')}</h2><button type="button" disabled={busy} onClick={close} aria-label={t('ai.closeDetail')}>{t('ai.closeDetail')} ×</button></div>
    {confirmClose && <div className="ai-notice"><p>{t('ai.closeUnsaved')}</p><button onClick={() => setConfirmClose(false)}>{t('ai.keepEditing')}</button> <button disabled={busy} onClick={onClose}>{t('ai.discardClose')}</button></div>}
    <div className="ai-job-modal-body">{publishedVersion && job.rules_version < publishedVersion && <p className="ai-notice">{t('ai.olderRules', {version: job.rules_version, current: publishedVersion})}</p>}
      <AiJobDetail key={`${job.id}:${job.revision}`} job={job} onDirtyChange={setDirty} onBusyChange={setBusy} onChange={value => { setDirty(false); setConfirmClose(false); onChange(value); }} />
    </div>
  </div></div>, document.body);
}
