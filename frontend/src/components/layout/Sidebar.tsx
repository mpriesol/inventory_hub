import React, { useState, useEffect } from 'react';
import { NavLink, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { setLanguage, getLanguage } from '../../i18n';
import {
  LayoutDashboard,
  PackageCheck,
  Package,
  BarChart3,
  Factory,
  ShoppingCart,
  Settings,
  ChevronRight,
  Globe,
} from 'lucide-react';

interface NavItem {
  id: string;
  nameKey: string;
  href: string;
  icon: React.ElementType;
  badge?: number | null;
}

const navigation: NavItem[] = [
  { id: 'dashboard', nameKey: 'nav.dashboard', href: '/', icon: LayoutDashboard, badge: null },
  { id: 'receiving', nameKey: 'nav.receiving', href: '/receiving', icon: PackageCheck, badge: 3 },
  { id: 'products', nameKey: 'nav.products', href: '/products', icon: Package, badge: null },
  { id: 'stock', nameKey: 'nav.stock', href: '/stock', icon: BarChart3, badge: 23 },
  { id: 'suppliers', nameKey: 'nav.suppliers', href: '/suppliers', icon: Factory, badge: null },
  { id: 'shops', nameKey: 'nav.shops', href: '/shops', icon: ShoppingCart, badge: null },
  { id: 'settings', nameKey: 'nav.settings', href: '/settings', icon: Settings, badge: null },
];

export function Sidebar() {
  const location = useLocation();
  const { t, i18n } = useTranslation();
  const [currentLang, setCurrentLang] = useState(getLanguage());

  useEffect(() => {
    setCurrentLang(i18n.language);
  }, [i18n.language]);

  const toggleLanguage = () => {
    const newLang = currentLang === 'sk' ? 'en' : 'sk';
    setLanguage(newLang);
    setCurrentLang(newLang);
  };

  const isActive = (href: string) => {
    if (href === '/') {
      return location.pathname === '/';
    }
    return location.pathname.startsWith(href);
  };

  return (
    <aside
      className="fixed left-0 top-0 h-screen flex flex-col border-r"
      style={{
        width: 'var(--sidebar-width)',
        backgroundColor: 'var(--color-bg-primary)',
        borderColor: 'var(--color-border-subtle)',
      }}
    >
      {/* Logo */}
      <div
        className="flex items-center gap-3 px-4 py-4 border-b"
        style={{ borderColor: 'var(--color-border-subtle)' }}
      >
        <div
          className="w-9 h-9 rounded-lg flex items-center justify-center font-bold text-sm"
          style={{
            backgroundColor: 'var(--color-accent)',
            color: 'var(--color-text-inverse)',
          }}
        >
          IH
        </div>
        <div>
          <div
            className="font-semibold text-sm"
            style={{
              fontFamily: 'var(--font-display)',
              color: 'var(--color-text-primary)',
            }}
          >
            Inventory Hub
          </div>
          <div
            className="text-xs"
            style={{ color: 'var(--color-text-tertiary)' }}
          >
            BikeTrek + xTrek
          </div>
        </div>
      </div>

      {/* Navigation */}
      <nav className="flex-1 p-2 space-y-1 overflow-y-auto">
        {navigation.map((item) => {
          const Icon = item.icon;
          const active = isActive(item.href);

          return (
            <NavLink
              key={item.id}
              to={item.href}
              className="flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm transition-all"
              style={{
                backgroundColor: active ? 'var(--color-accent-subtle)' : 'transparent',
                color: active ? 'var(--color-accent)' : 'var(--color-text-secondary)',
                border: active ? '1px solid var(--color-border-accent)' : '1px solid transparent',
              }}
              onMouseEnter={(e) => {
                if (!active) {
                  e.currentTarget.style.backgroundColor = 'var(--color-bg-secondary)';
                  e.currentTarget.style.color = 'var(--color-text-primary)';
                }
              }}
              onMouseLeave={(e) => {
                if (!active) {
                  e.currentTarget.style.backgroundColor = 'transparent';
                  e.currentTarget.style.color = 'var(--color-text-secondary)';
                }
              }}
            >
              <Icon size={18} />
              <span className="flex-1">{t(item.nameKey)}</span>
              {item.badge !== null && item.badge > 0 && (
                <span
                  className="text-xs px-1.5 py-0.5 rounded-full"
                  style={{
                    backgroundColor: active
                      ? 'var(--color-accent-muted)'
                      : 'var(--color-bg-tertiary)',
                    color: active
                      ? 'var(--color-accent)'
                      : 'var(--color-text-secondary)',
                  }}
                >
                  {item.badge}
                </span>
              )}
            </NavLink>
          );
        })}
      </nav>

      {/* Language Toggle */}
      <div
        className="px-3 py-2 border-t"
        style={{ borderColor: 'var(--color-border-subtle)' }}
      >
        <button
          onClick={toggleLanguage}
          className="flex items-center gap-3 w-full px-3 py-2 rounded-lg text-sm transition-colors"
          style={{ color: 'var(--color-text-secondary)' }}
          onMouseEnter={(e) => {
            e.currentTarget.style.backgroundColor = 'var(--color-bg-secondary)';
            e.currentTarget.style.color = 'var(--color-text-primary)';
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.backgroundColor = 'transparent';
            e.currentTarget.style.color = 'var(--color-text-secondary)';
          }}
        >
          <Globe size={18} />
          <span className="flex-1 text-left">{t('settings.language')}</span>
          <span
            className="text-xs px-2 py-0.5 rounded font-medium"
            style={{
              backgroundColor: 'var(--color-accent-subtle)',
              color: 'var(--color-accent)',
            }}
          >
            {currentLang.toUpperCase()}
          </span>
        </button>
      </div>

      {/* User Section */}
      <div
        className="p-3 border-t"
        style={{ borderColor: 'var(--color-border-subtle)' }}
      >
        <div
          className="flex items-center gap-3 px-2 py-2 rounded-lg cursor-pointer transition-colors"
          style={{ backgroundColor: 'transparent' }}
          onMouseEnter={(e) => {
            e.currentTarget.style.backgroundColor = 'var(--color-bg-secondary)';
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.backgroundColor = 'transparent';
          }}
        >
          <div
            className="w-8 h-8 rounded-full flex items-center justify-center font-medium text-xs"
            style={{
              background: 'linear-gradient(135deg, #f59e0b, #ea580c)',
              color: 'var(--color-text-inverse)',
            }}
          >
            M
          </div>
          <div className="flex-1 min-w-0">
            <div
              className="text-sm truncate"
              style={{ color: 'var(--color-text-primary)' }}
            >
              Miro
            </div>
            <div
              className="text-xs"
              style={{ color: 'var(--color-text-tertiary)' }}
            >
              Admin
            </div>
          </div>
          <ChevronRight
            size={16}
            style={{ color: 'var(--color-text-tertiary)' }}
          />
        </div>
      </div>
    </aside>
  );
}

export default Sidebar;
