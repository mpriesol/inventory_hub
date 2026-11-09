import React, { useEffect, useRef, useState } from 'react';
import { Outlet, useLocation } from 'react-router-dom';
import { Menu } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Sidebar } from './Sidebar';
import './Layout.css';

export function Layout() {
  const { pathname } = useLocation();
  const { t } = useTranslation();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  useEffect(() => { setMenuOpen(false); }, [pathname]);
  useEffect(() => {
    const desktop = window.matchMedia?.('(min-width: 801px)');
    const changed = () => { if (desktop?.matches) setMenuOpen(false); };
    desktop?.addEventListener('change', changed);
    return () => desktop?.removeEventListener('change', changed);
  }, []);
  const wide = /^\/(stock|receiving|invoices|orders|product-import|product-trace|suppliers)(?:\/|$)/.test(pathname);
  return (
    <div
      className="min-h-screen"
      style={{ backgroundColor: 'var(--color-bg-primary)' }}
    >
      <header className="hub-mobile-header">
        <button ref={menuButton} type="button" aria-label={t('nav.openMenu')} aria-expanded={menuOpen}
          aria-controls="hub-navigation" onClick={() => setMenuOpen(open => !open)}><Menu size={22} /></button>
        <strong>Inventory Hub</strong>
      </header>
      {menuOpen && <button type="button" className="hub-mobile-backdrop" aria-label={t('nav.closeMenu')} tabIndex={-1} onClick={() => setMenuOpen(false)} />}
      <Sidebar mobileOpen={menuOpen} onClose={() => setMenuOpen(false)} returnFocus={menuButton} />
      
      {/* Main Content */}
      <main
        className="min-h-screen hub-main"
      >
        <div className={`${wide ? 'w-full min-w-0' : 'max-w-7xl mx-auto'} animate-fade-in`}>
          <Outlet />
        </div>
      </main>
    </div>
  );
}

export default Layout;
