import React from 'react';
import { useParams, useNavigate, useSearchParams } from 'react-router-dom';
import { StockCard } from '../components/product/StockCard';

/** Direct and legacy links open exactly the same card as an in-place link. */
export function ProductDetailPage() {
  const { sku, productId: idText } = useParams<{ sku: string; productId: string }>();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const productId = idText && /^\d+$/.test(idText) && Number.isSafeInteger(Number(idText)) && Number(idText) > 0 ? Number(idText) : undefined;
  const warehouseCode = params.has('warehouse') ? params.get('warehouse') : undefined;
  return <StockCard key={`${idText || sku}:${warehouseCode}`} productId={productId} sku={sku}
    warehouseCode={warehouseCode} onClose={() => navigate('/stock')} />;
}

export default ProductDetailPage;
