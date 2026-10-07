export interface WarehouseChoice {
  code: string;
  is_default?: boolean;
  is_active?: boolean;
}

/** Server configuration decides the default; ordering and display names do not. */
export function defaultWarehouse<T extends WarehouseChoice>(rows: readonly T[]): T | undefined {
  return rows.find(row => row.is_default === true && row.is_active !== false);
}

export function defaultWarehouseCode(rows: readonly WarehouseChoice[]): string {
  return defaultWarehouse(rows)?.code || '';
}
