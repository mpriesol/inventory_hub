import React, { useSyncExternalStore } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { accessRevision, hubUnlocked, subscribeAccess } from '../api/access';

/** Private screens never mount (or start data requests) before authentication. */
export function RequireHubSession({ children }: { children: React.ReactNode }) {
  useSyncExternalStore(subscribeAccess, accessRevision);
  const location = useLocation();
  if (!hubUnlocked() && location.pathname !== '/login') {
    return <Navigate to="/login" replace state={{ returnTo: location.pathname + location.search + location.hash }} />;
  }
  return <>{children}</>;
}
