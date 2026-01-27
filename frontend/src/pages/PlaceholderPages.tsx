import React from 'react';
import { useNavigate } from 'react-router-dom';
import { Construction } from 'lucide-react';
import { Button } from '../components/ui/Button.new';

interface PlaceholderPageProps {
  title: string;
  description: string;
}

export function PlaceholderPage({ title, description }: PlaceholderPageProps) {
  const navigate = useNavigate();

  return (
    <div className="space-y-6">
      <div>
        <h1
          className="text-2xl font-semibold"
          style={{
            fontFamily: 'var(--font-display)',
            color: 'var(--color-text-primary)',
          }}
        >
          {title}
        </h1>
        <p
          className="text-sm mt-1"
          style={{ color: 'var(--color-text-tertiary)' }}
        >
          {description}
        </p>
      </div>

      <div
        className="rounded-xl border p-12 text-center"
        style={{
          backgroundColor: 'var(--color-bg-secondary)',
          borderColor: 'var(--color-border-subtle)',
        }}
      >
        <Construction
          size={48}
          className="mx-auto mb-4"
          style={{ color: 'var(--color-text-tertiary)' }}
        />
        <h2
          className="text-lg font-medium mb-2"
          style={{ color: 'var(--color-text-primary)' }}
        >
          V príprave
        </h2>
        <p
          className="text-sm max-w-md mx-auto"
          style={{ color: 'var(--color-text-tertiary)' }}
        >
          Táto sekcia je momentálne v príprave. Čoskoro bude dostupná s plnou funkcionalitou.
        </p>
        <div className="mt-6">
          <Button variant="secondary" onClick={() => navigate('/')}>
            ← Späť na dashboard
          </Button>
        </div>
      </div>
    </div>
  );
}

// Export specific pages
export function ProductsPage() {
  return (
    <PlaceholderPage
      title="Produkty"
      description="Katalóg všetkých produktov v systéme"
    />
  );
}

export function SuppliersPage() {
  return (
    <PlaceholderPage
      title="Dodávatelia"
      description="Správa dodávateľov a ich konfigurácií"
    />
  );
}

export function ShopsPage() {
  return (
    <PlaceholderPage
      title="Shopy"
      description="Pripojené e-shopy a synchronizácia"
    />
  );
}

export function SettingsPage() {
  return (
    <PlaceholderPage
      title="Nastavenia"
      description="Konfigurácia systému"
    />
  );
}

export default PlaceholderPage;
