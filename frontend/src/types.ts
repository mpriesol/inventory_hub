export interface Supplier {
  id?: number;
  name: string;
  supplier_code?: string;
  adapter?: string;
}

export interface ListedFile {
  name: string;
  href?: string;
  path?: string;
  size_bytes?: number;
  modified?: string; // ISO
}
