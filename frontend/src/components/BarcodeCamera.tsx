import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Camera, Flashlight, Pause } from 'lucide-react';
import { Button } from './ui/Button.new';
import type { IScannerControls } from '@zxing/browser';

/** A decoded video frame is not a new physical piece. Rearm explicitly per piece. */
export function BarcodeCamera({ disabled = false, onScan }: { disabled?: boolean; onScan: (code: string) => void }) {
  const { t } = useTranslation();
  const video = useRef<HTMLVideoElement>(null);
  const controls = useRef<IScannerControls | null>(null);
  const generation = useRef(0);
  const armed = useRef(true);
  const props = useRef({ disabled, onScan }); props.current = { disabled, onScan };
  const [active, setActive] = useState(false);
  const [starting, setStarting] = useState(false);
  const [waiting, setWaiting] = useState(false);
  const [torch, setTorch] = useState(false);
  const [canTorch, setCanTorch] = useState(false);
  const [error, setError] = useState('');

  function stop() {
    generation.current++;
    controls.current?.stop(); controls.current = null;
    const stream = video.current?.srcObject as MediaStream | null;
    stream?.getTracks().forEach(track => track.stop());
    if (video.current) video.current.srcObject = null;
    setActive(false); setStarting(false); setTorch(false); setCanTorch(false); setWaiting(false);
    armed.current = true;
  }
  useEffect(() => {
    const hidden = () => { if (document.hidden) stop(); };
    document.addEventListener('visibilitychange', hidden);
    window.addEventListener('pagehide', stop);
    return () => { document.removeEventListener('visibilitychange', hidden); window.removeEventListener('pagehide', stop); stop(); };
  }, []);
  useEffect(() => { if (disabled && (active || starting)) stop(); }, [disabled]);

  async function start() {
    if (starting || props.current.disabled) return;
    if (active) { stop(); return; }
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      setError(t('receiving.camera.secureRequired')); return;
    }
    setStarting(true); setError(''); armed.current = true; setWaiting(false);
    const current = ++generation.current;
    try {
      const [{ BrowserMultiFormatReader }, { BarcodeFormat, DecodeHintType }] = await Promise.all([
        import('@zxing/browser'), import('@zxing/library'),
      ]);
      if (current !== generation.current || props.current.disabled || !video.current) return;
      const reader = new BrowserMultiFormatReader(new Map([[DecodeHintType.POSSIBLE_FORMATS,
        [BarcodeFormat.EAN_13, BarcodeFormat.EAN_8, BarcodeFormat.UPC_A, BarcodeFormat.UPC_E, BarcodeFormat.CODE_128]]]),
        { delayBetweenScanAttempts: 120, delayBetweenScanSuccess: 300 });
      const next = await reader.decodeFromConstraints({ audio: false, video: {
        facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 720 },
      } }, video.current, result => {
        if (!result || current !== generation.current || props.current.disabled || !armed.current) return;
        const code = result.getText().trim();
        if (!code) return;
        armed.current = false; setWaiting(true);
        try { navigator.vibrate?.(60); } catch { /* Optional device feedback. */ }
        props.current.onScan(code);
      });
      if (current !== generation.current || props.current.disabled) { next.stop(); return; }
      controls.current = next; setActive(true); setCanTorch(!!next.switchTorch);
    } catch (failure) {
      if (current !== generation.current) return;
      stop();
      setError(t((failure as Error)?.name === 'NotAllowedError' ? 'receiving.camera.permission' : 'receiving.camera.failed'));
    } finally { if (current === generation.current) setStarting(false); }
  }
  async function toggleTorch() {
    try { await controls.current?.switchTorch?.(!torch); setTorch(value => !value); }
    catch { setError(t('receiving.camera.torchFailed')); }
  }
  return <section className="receiving-camera" aria-label={t('receiving.camera.title')}>
    <div className="receiving-camera-preview" hidden={!active && !starting}>
      <video ref={video} muted playsInline aria-label={t('receiving.camera.preview')} />
      {waiting && <div className="receiving-camera-captured" role="status">{t('receiving.camera.captured')}</div>}
    </div>
    <div className="receiving-camera-controls">
      <Button type="button" data-testid="receiving-camera-toggle" variant="secondary" disabled={disabled || starting} onClick={() => void start()}>
        {active ? <Pause size={18} /> : <Camera size={18} />}{t(starting ? 'common.loading' : active ? 'receiving.camera.stop' : 'receiving.camera.start')}
      </Button>
      {active && canTorch && <Button type="button" variant="secondary" onClick={() => void toggleTorch()} aria-pressed={torch}><Flashlight size={18} />{t('receiving.camera.torch')}</Button>}
      {active && waiting && <Button type="button" data-testid="receiving-camera-next" disabled={disabled} onClick={() => { armed.current = true; setWaiting(false); }}>{t('receiving.camera.next')}</Button>}
    </div>
    {active && <p className="receiving-muted">{t('receiving.camera.help')}</p>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
