import React, { useId, useRef, useState } from 'react';
import './HelpHint.css';

/** Mouse, keyboard and touch help. Essential status/errors stay outside hints. */
export function HelpHint({ label, children }: { label: string; children: React.ReactNode }) {
  const id = useId(), button = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false), [pinned, setPinned] = useState(false);
  const [position, setPosition] = useState<React.CSSProperties>({});
  function show() {
    const rect = button.current?.getBoundingClientRect();
    if (rect) {
      const width = Math.min(352, window.innerWidth - 24);
      setPosition({ width, left: Math.max(12, Math.min(rect.left, window.innerWidth - width - 12)),
        ...(window.innerHeight - rect.bottom < 220 ? { bottom: window.innerHeight - rect.top + 8 } : { top: rect.bottom + 8 }) });
    }
    setOpen(true);
  }
  function close() { setPinned(false); setOpen(false); }
  return <span className="help-hint" onMouseEnter={show} onMouseLeave={() => { if (!pinned) setOpen(false); }}
    onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) close(); }}
    onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); close(); } }}>
    <button ref={button} type="button" className="help-hint-button" aria-label={label} aria-expanded={open}
      aria-describedby={open ? id : undefined} onFocus={show} onClick={() => { if (pinned) close(); else { setPinned(true); show(); } }}>?</button>
    <span id={id} role="tooltip" className="help-hint-content" hidden={!open} style={position}>{children}</span>
  </span>;
}
