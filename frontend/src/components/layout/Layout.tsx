import React from 'react';
import { Outlet } from 'react-router-dom';
import { Sidebar } from './Sidebar';

export function Layout() {
  return (
    <div
      className="min-h-screen"
      style={{ backgroundColor: 'var(--color-bg-primary)' }}
    >
      <Sidebar />
      
      {/* Main Content */}
      <main
        className="min-h-screen"
        style={{
          marginLeft: 'var(--sidebar-width)',
          padding: 'var(--spacing-6)',
        }}
      >
        <div className="max-w-7xl mx-auto animate-fade-in">
          <Outlet />
        </div>
      </main>
    </div>
  );
}

export default Layout;
