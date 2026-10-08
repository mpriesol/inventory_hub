import React from 'react';
import { AccountPage } from './pages/AccountPage';
import { SessionBootstrap } from './components/SessionBootstrap';
import { AvailabilitySyncPage } from './pages/AvailabilitySyncPage';
import { FifoCostSyncPage } from './pages/FifoCostSyncPage';
import { AiContentPage } from './pages/AiContentPage';
import { ProductImportPage } from './pages/ProductImportPage';
import { FeedMappingPage } from './pages/FeedMappingPage';
import { createBrowserRouter, createRoutesFromElements, RouterProvider, Route, Navigate } from 'react-router-dom';

// Styles
import './styles/design-system.css';

// Layout
import { Layout } from './components/layout';

// Pages
import {
  DashboardPage,
  ReceivingPage,
  ReceivingSessionPage,
  StockHistoryPage,
  OpeningStockPage,
  OrdersAuditPage,
  OrdersStockPage,
  OrdersInboxPage,
  StockSettingsPage,
  StockPublicationPage,
  ProductEditorPage,
  SuppliersPage,
  SupplierCatalogPage,
  ShopsPage,
  SettingsPage,
  InvoicesPage,
  InvoiceDetailPage,
  ProductDetailPage,
} from './pages';

// A data router lets editable imports use the supported navigation blocker.
const router = createBrowserRouter(createRoutesFromElements(
        <Route element={<SessionBootstrap><Layout /></SessionBootstrap>}>
          {/* Dashboard */}
          <Route path="/" element={<DashboardPage />} />

          {/* Invoices - between Dashboard and Receiving */}
          <Route path="/invoices" element={<InvoicesPage />} />
          <Route path="/invoices/:invoiceId" element={<InvoiceDetailPage />} />

          {/* Receiving */}
          <Route path="/receiving" element={<ReceivingPage />} />
          <Route path="/receiving/:invoiceId" element={<ReceivingSessionPage />} />

          {/* Stock */}
          <Route path="/stock" element={<ProductEditorPage />} />
          <Route path="/stock/movements" element={<StockHistoryPage />} />
          <Route path="/stock/opening" element={<OpeningStockPage />} />
          <Route path="/stock/publication" element={<StockPublicationPage />} />
          <Route path="/orders" element={<OrdersAuditPage />} />
          <Route path="/orders/stock" element={<OrdersStockPage />} />
          <Route path="/orders/inbox" element={<OrdersInboxPage />} />

          {/* Products */}
          <Route path="/products" element={<Navigate to="/stock" replace />} />
          <Route path="/products/:sku" element={<ProductDetailPage />} />
          <Route path="/product-import" element={<ProductImportPage />} />

          {/* Suppliers */}
          <Route path="/suppliers" element={<SuppliersPage />} />
          <Route path="/suppliers/:supplier/catalog" element={<SupplierCatalogPage />} />
          <Route path="/suppliers/:supplier/feed-mapping" element={<FeedMappingPage />} />

          {/* Shops */}
          <Route path="/shops" element={<ShopsPage />} />

          <Route path="/login" element={<AccountPage />} />
          <Route path="/settings/users" element={<AccountPage />} />
          {/* Settings */}
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/settings/availability" element={<AvailabilitySyncPage />} />
          <Route path="/settings/purchase-costs" element={<FifoCostSyncPage />} />
          <Route path="/settings/stock" element={<StockSettingsPage />} />
          <Route path="/settings/ai-content" element={<AiContentPage />} />
          <Route path="/ai-content" element={<AiContentPage />} />

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
));

function App() {
  return <RouterProvider router={router} />;
}

export default App;
