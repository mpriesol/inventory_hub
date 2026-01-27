import React from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';

// Styles
import './styles/design-system.css';

// Layout
import { Layout } from './components/layout';

// Pages
import {
  DashboardPage,
  ReceivingPage,
  ReceivingSessionPage,
  StockPage,
  ProductsPage,
  SuppliersPage,
  ShopsPage,
  SettingsPage,
} from './pages';

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          {/* Dashboard */}
          <Route path="/" element={<DashboardPage />} />

          {/* Receiving */}
          <Route path="/receiving" element={<ReceivingPage />} />
          <Route path="/receiving/:invoiceId" element={<ReceivingSessionPage />} />

          {/* Stock */}
          <Route path="/stock" element={<StockPage />} />

          {/* Products */}
          <Route path="/products" element={<ProductsPage />} />

          {/* Suppliers */}
          <Route path="/suppliers" element={<SuppliersPage />} />

          {/* Shops */}
          <Route path="/shops" element={<ShopsPage />} />

          {/* Settings */}
          <Route path="/settings" element={<SettingsPage />} />

          {/* Fallback */}
          <Route
            path="*"
            element={
              <div
                className="flex items-center justify-center h-64"
                style={{ color: 'var(--color-text-tertiary)' }}
              >
                <div className="text-center">
                  <div className="text-4xl mb-2">404</div>
                  <div>Stránka nenájdená</div>
                </div>
              </div>
            }
          />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}

export default App;
