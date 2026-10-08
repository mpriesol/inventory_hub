import { useEffect, useRef } from 'react';
import { useBlocker } from 'react-router-dom';

/** Protect local editor changes for links, programmatic navigation and history Back. */
export function useUnsavedNavigationGuard(dirty: boolean, message: string) {
  const blocker = useBlocker(({ currentLocation, nextLocation }) => dirty &&
    `${currentLocation.pathname}${currentLocation.search}${currentLocation.hash}` !==
    `${nextLocation.pathname}${nextLocation.search}${nextLocation.hash}`);
  const prompted = useRef<string | null>(null);

  useEffect(() => {
    if (blocker.state !== 'blocked') { prompted.current = null; return; }
    // One confirmation per blocked transition, including StrictMode effect replay.
    if (prompted.current === blocker.location.key) return;
    prompted.current = blocker.location.key;
    if (!dirty || window.confirm(message)) blocker.proceed();
    else blocker.reset();
  }, [blocker, dirty, message]);

  useEffect(() => {
    if (!dirty) return;
    const leave = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', leave);
    return () => window.removeEventListener('beforeunload', leave);
  }, [dirty]);
}
