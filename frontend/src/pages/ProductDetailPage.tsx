// src/pages/ProductDetailPage.tsx — tenký wrapper nad zdieľaným ProductDetailView
import React from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { Button } from '../components/ui/Button.new';
import { ProductDetailView } from '../components/product/ProductDisplay';

export function ProductDetailPage() {
  const { sku } = useParams<{ sku: string }>();
  const navigate = useNavigate();

  return (
    <div className="p-6 max-w-5xl">
      <Button variant="secondary" size="sm" onClick={() => navigate('/stock')}>
        <ArrowLeft size={14} /> Späť na sklad
      </Button>
      <div className="mt-4">
        {sku ? <ProductDetailView sku={sku} /> : <div style={{ color: 'var(--color-error)' }}>Chýba SKU</div>}
      </div>
    </div>
  );
}

export default ProductDetailPage;
