// src/pages/SuppliersPage.tsx
// Complete suppliers management page with editor, history, and validation

import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  Package,
  Plus,
  Settings,
  RefreshCw,
  Upload,
  History,
  CheckCircle,
  AlertTriangle,
  XCircle,
  ChevronRight,
  Save,
  X,
  Eye,
  EyeOff,
  Code,
  FileText,
  Truck,
  Globe,
  Key,
  Trash2,
  RotateCcw,
  FileUp,
  Check,
  AlertCircle,
} from 'lucide-react';
import { Button } from '../components/ui/Button.new';
import { Modal } from '../components/ui/Modal';
import {
  listSuppliers,
  getSupplierConfig,
  updateSupplierConfig,
  createSupplier,
  getSupplierHistory,
  getSupplierHistoryVersion,
  restoreSupplierVersion,
  validateSupplierConfig,
  uploadInvoice,
  deleteSupplier,
  SupplierSummary,
  SupplierConfig,
  SupplierHistoryEntry,
  ValidationResult,
} from '../api/suppliers';

// -----------------------------------------------------------------------------
// Supplier List Component
// -----------------------------------------------------------------------------
interface SupplierCardProps {
  supplier: SupplierSummary;
  onEdit: (code: string) => void;
}

function SupplierCard({ supplier, onEdit }: SupplierCardProps) {
  const strategyLabels: Record<string, string> = {
    'paul-lange-web': 'Auto-download',
    'web': 'Web scraping',
    'manual': 'Manuálny upload',
    'api': 'API',
    'disabled': 'Vypnuté',
  };

  const feedModeLabels: Record<string, string> = {
    'remote': 'Online',
    'local': 'Lokálny',
    'none': 'Žiadny',
  };

  return (
    <div
      className="rounded-xl border p-4 transition-all cursor-pointer group"
      style={{
        backgroundColor: 'var(--color-bg-secondary)',
        borderColor: 'var(--color-border-subtle)',
      }}
      onClick={() => onEdit(supplier.code)}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = 'var(--color-border-accent)';
        e.currentTarget.style.backgroundColor = 'var(--color-bg-tertiary)';
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = 'var(--color-border-subtle)';
        e.currentTarget.style.backgroundColor = 'var(--color-bg-secondary)';
      }}
    >
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-3">
          <div
            className="w-10 h-10 rounded-lg flex items-center justify-center"
            style={{ backgroundColor: 'var(--color-accent-subtle)' }}
          >
            <Package size={20} style={{ color: 'var(--color-accent)' }} />
          </div>
          <div>
            <h3
              className="font-medium"
              style={{ color: 'var(--color-text-primary)', fontFamily: 'var(--font-display)' }}
            >
              {supplier.name}
            </h3>
            <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              {supplier.product_prefix ? `${supplier.product_prefix} prefix` : 'Bez prefixu'}
              {' • '}
              {supplier.invoice_count} faktúr
              {' • '}
              Feed: {feedModeLabels[supplier.feed_mode] || supplier.feed_mode}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span
            className={`px-2 py-0.5 rounded-full text-xs font-medium ${
              supplier.is_active
                ? 'bg-green-500/10 text-green-400'
                : 'bg-gray-500/10 text-gray-400'
            }`}
          >
            {supplier.is_active ? '● Aktívny' : '○ Neaktívny'}
          </span>
          <ChevronRight
            size={18}
            className="opacity-0 group-hover:opacity-100 transition-opacity"
            style={{ color: 'var(--color-text-tertiary)' }}
          />
        </div>
      </div>
      <div className="mt-3 flex items-center gap-4 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
        <span>{strategyLabels[supplier.download_strategy] || supplier.download_strategy}</span>
        {supplier.last_invoice_date && (
          <span>Posledná faktúra: {supplier.last_invoice_date}</span>
        )}
      </div>
    </div>
  );
}

// -----------------------------------------------------------------------------
// Create Supplier Modal
// -----------------------------------------------------------------------------
interface CreateSupplierModalProps {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
}

function CreateSupplierModal({ open, onClose, onCreated }: CreateSupplierModalProps) {
  const [code, setCode] = useState('');
  const [name, setName] = useState('');
  const [prefix, setPrefix] = useState('');
  const [strategy, setStrategy] = useState('manual');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!code.trim() || !name.trim()) {
      setError('Kód a názov sú povinné');
      return;
    }

    setLoading(true);
    setError(null);
    try {
      await createSupplier({
        code: code.trim(),
        name: name.trim(),
        product_prefix: prefix.trim(),
        download_strategy: strategy,
      });
      onCreated();
      onClose();
      setCode('');
      setName('');
      setPrefix('');
      setStrategy('manual');
    } catch (err: any) {
      setError(err.message || 'Nepodarilo sa vytvoriť dodávateľa');
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="Nový dodávateľ" align="center">
      <form onSubmit={handleSubmit} className="space-y-4">
        {error && (
          <div className="p-3 rounded-lg bg-red-500/10 border border-red-500/30 text-red-400 text-sm">
            {error}
          </div>
        )}

        <div>
          <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
            Kód dodávateľa *
          </label>
          <input
            type="text"
            value={code}
            onChange={(e) => setCode(e.target.value.toLowerCase().replace(/\s+/g, '-'))}
            placeholder="napr. shimano-europe"
            className="w-full"
            autoFocus
          />
          <p className="text-xs mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
            Použije sa ako názov priečinka. Len malé písmená a pomlčky.
          </p>
        </div>

        <div>
          <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
            Názov *
          </label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="napr. Shimano Europe"
            className="w-full"
          />
        </div>

        <div>
          <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
            Prefix produktov
          </label>
          <input
            type="text"
            value={prefix}
            onChange={(e) => setPrefix(e.target.value.toUpperCase())}
            placeholder="napr. SE-"
            className="w-full"
          />
        </div>

        <div>
          <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
            Stratégia sťahovania faktúr
          </label>
          <select
            value={strategy}
            onChange={(e) => setStrategy(e.target.value)}
            className="w-full"
          >
            <option value="manual">Manuálny upload</option>
            <option value="web">Web scraping</option>
            <option value="api">API</option>
            <option value="disabled">Vypnuté</option>
          </select>
        </div>

        <div className="flex gap-3 justify-end pt-2">
          <Button type="button" variant="secondary" onClick={onClose}>
            Zrušiť
          </Button>
          <Button type="submit" loading={loading}>
            Vytvoriť
          </Button>
        </div>
      </form>
    </Modal>
  );
}

// -----------------------------------------------------------------------------
// Supplier Editor
// -----------------------------------------------------------------------------
interface SupplierEditorProps {
  code: string;
  onClose: () => void;
  onSaved: () => void;
}

type EditorTab = 'general' | 'feeds' | 'invoices' | 'mapping' | 'json' | 'history';

function SupplierEditor({ code, onClose, onSaved }: SupplierEditorProps) {
  const [activeTab, setActiveTab] = useState<EditorTab>('general');
  const [config, setConfig] = useState<SupplierConfig | null>(null);
  const [originalConfig, setOriginalConfig] = useState<SupplierConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hasChanges, setHasChanges] = useState(false);
  const [showPasswords, setShowPasswords] = useState(false);

  // History state
  const [history, setHistory] = useState<SupplierHistoryEntry[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [selectedHistoryVersion, setSelectedHistoryVersion] = useState<string | null>(null);
  const [historyConfig, setHistoryConfig] = useState<SupplierConfig | null>(null);

  // Validation state
  const [validation, setValidation] = useState<ValidationResult | null>(null);
  const [validating, setValidating] = useState(false);

  // Upload state
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Load config
  useEffect(() => {
    loadConfig();
  }, [code]);

  const loadConfig = async () => {
    setLoading(true);
    setError(null);
    try {
      const cfg = await getSupplierConfig(code);
      setConfig(cfg);
      setOriginalConfig(JSON.parse(JSON.stringify(cfg)));
      setHasChanges(false);
    } catch (err: any) {
      setError(err.message || 'Nepodarilo sa načítať konfiguráciu');
    } finally {
      setLoading(false);
    }
  };

  // Load history when tab changes
  useEffect(() => {
    if (activeTab === 'history') {
      loadHistory();
    }
  }, [activeTab, code]);

  const loadHistory = async () => {
    setHistoryLoading(true);
    try {
      const h = await getSupplierHistory(code);
      setHistory(h);
    } catch (err) {
      console.error('Failed to load history:', err);
    } finally {
      setHistoryLoading(false);
    }
  };

  // Update config helper
  const updateConfig = useCallback((updater: (cfg: SupplierConfig) => SupplierConfig) => {
    setConfig((prev) => {
      if (!prev) return prev;
      const updated = updater({ ...prev });
      setHasChanges(JSON.stringify(updated) !== JSON.stringify(originalConfig));
      return updated;
    });
  }, [originalConfig]);

  // Save config
  const handleSave = async () => {
    if (!config) return;
    setSaving(true);
    setError(null);
    try {
      await updateSupplierConfig(code, config);
      setOriginalConfig(JSON.parse(JSON.stringify(config)));
      setHasChanges(false);
      onSaved();
    } catch (err: any) {
      setError(err.message || 'Nepodarilo sa uložiť');
    } finally {
      setSaving(false);
    }
  };

  // Validate config
  const handleValidate = async (checkUrls: boolean = false) => {
    setValidating(true);
    try {
      const result = await validateSupplierConfig(code, checkUrls);
      setValidation(result);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setValidating(false);
    }
  };

  // Restore from history
  const handleRestore = async (version: string) => {
    if (!confirm('Naozaj chcete obnoviť túto verziu? Aktuálna konfigurácia bude uložená do histórie.')) {
      return;
    }
    try {
      const restored = await restoreSupplierVersion(code, version);
      setConfig(restored);
      setOriginalConfig(JSON.parse(JSON.stringify(restored)));
      setHasChanges(false);
      setActiveTab('general');
      await loadHistory();
    } catch (err: any) {
      setError(err.message);
    }
  };

  // View history version
  const handleViewHistoryVersion = async (version: string) => {
    try {
      const cfg = await getSupplierHistoryVersion(code, version);
      setHistoryConfig(cfg);
      setSelectedHistoryVersion(version);
    } catch (err: any) {
      setError(err.message);
    }
  };

  // Handle file upload
  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    setUploading(true);
    try {
      await uploadInvoice(code, file);
      alert(`Súbor ${file.name} bol úspešne nahraný`);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setUploading(false);
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
    }
  };

  // Tab definitions
  const tabs: { id: EditorTab; label: string; icon: React.ReactNode }[] = [
    { id: 'general', label: 'Obecné', icon: <Settings size={16} /> },
    { id: 'feeds', label: 'Feedy', icon: <Globe size={16} /> },
    { id: 'invoices', label: 'Faktúry', icon: <FileText size={16} /> },
    { id: 'mapping', label: 'Mapovanie', icon: <Truck size={16} /> },
    { id: 'json', label: 'JSON', icon: <Code size={16} /> },
    { id: 'history', label: 'História', icon: <History size={16} /> },
  ];

  if (loading) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
        <div className="animate-pulse" style={{ color: 'var(--color-text-tertiary)' }}>
          Načítavam...
        </div>
      </div>
    );
  }

  if (!config) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
        <div className="p-6 rounded-xl" style={{ backgroundColor: 'var(--color-bg-secondary)' }}>
          <p style={{ color: 'var(--color-error)' }}>{error || 'Konfigurácia nenájdená'}</p>
          <Button variant="secondary" onClick={onClose} className="mt-4">
            Zavrieť
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="fixed inset-0 z-50">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/60" onClick={onClose} />

      {/* Editor panel */}
      <div
        className="absolute inset-y-0 right-0 w-full max-w-4xl overflow-hidden flex flex-col"
        style={{ backgroundColor: 'var(--color-bg-primary)' }}
      >
        {/* Header */}
        <div
          className="flex items-center justify-between px-6 py-4 border-b"
          style={{ borderColor: 'var(--color-border-subtle)' }}
        >
          <div className="flex items-center gap-3">
            <button
              onClick={onClose}
              className="p-2 rounded-lg transition-colors"
              style={{ color: 'var(--color-text-secondary)' }}
              onMouseEnter={(e) => (e.currentTarget.style.backgroundColor = 'var(--color-bg-secondary)')}
              onMouseLeave={(e) => (e.currentTarget.style.backgroundColor = 'transparent')}
            >
              <X size={20} />
            </button>
            <div>
              <h2
                className="text-lg font-semibold"
                style={{ color: 'var(--color-text-primary)', fontFamily: 'var(--font-display)' }}
              >
                {config.name || code}
              </h2>
              <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                {code}
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            {hasChanges && (
              <span className="text-xs px-2 py-1 rounded-full bg-amber-500/20 text-amber-400">
                Neuložené zmeny
              </span>
            )}
            <Button
              variant="secondary"
              size="sm"
              onClick={() => handleValidate(false)}
              loading={validating}
              icon={<CheckCircle size={16} />}
            >
              Validovať
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => handleValidate(true)}
              loading={validating}
              icon={<Globe size={16} />}
            >
              Test URL
            </Button>
            <Button
              onClick={handleSave}
              loading={saving}
              disabled={!hasChanges}
              icon={<Save size={16} />}
            >
              Uložiť
            </Button>
          </div>
        </div>

        {/* Error/Validation display */}
        {(error || validation) && (
          <div className="px-6 py-3 space-y-2">
            {error && (
              <div className="p-3 rounded-lg bg-red-500/10 border border-red-500/30 text-red-400 text-sm flex items-center gap-2">
                <XCircle size={16} />
                {error}
                <button onClick={() => setError(null)} className="ml-auto">
                  <X size={14} />
                </button>
              </div>
            )}
            {validation && (
              <div
                className={`p-3 rounded-lg border text-sm ${
                  validation.valid
                    ? 'bg-green-500/10 border-green-500/30 text-green-400'
                    : 'bg-amber-500/10 border-amber-500/30 text-amber-400'
                }`}
              >
                <div className="flex items-center gap-2 font-medium">
                  {validation.valid ? <CheckCircle size={16} /> : <AlertTriangle size={16} />}
                  {validation.valid ? 'Konfigurácia je platná' : 'Nájdené problémy'}
                </div>
                {validation.errors.length > 0 && (
                  <ul className="mt-2 space-y-1 text-red-400">
                    {validation.errors.map((e, i) => (
                      <li key={i}>• {e}</li>
                    ))}
                  </ul>
                )}
                {validation.warnings.length > 0 && (
                  <ul className="mt-2 space-y-1">
                    {validation.warnings.map((w, i) => (
                      <li key={i}>⚠ {w}</li>
                    ))}
                  </ul>
                )}
                {validation.feed_url_reachable !== null && (
                  <p className="mt-2">
                    Feed URL: {validation.feed_url_reachable ? '✓ Dostupná' : '✗ Nedostupná'}
                  </p>
                )}
                {validation.login_url_reachable !== null && (
                  <p>
                    Login URL: {validation.login_url_reachable ? '✓ Dostupná' : '✗ Nedostupná'}
                  </p>
                )}
                <button
                  onClick={() => setValidation(null)}
                  className="mt-2 text-xs underline"
                >
                  Zavrieť
                </button>
              </div>
            )}
          </div>
        )}

        {/* Tabs */}
        <div
          className="flex gap-1 px-6 py-2 border-b overflow-x-auto"
          style={{ borderColor: 'var(--color-border-subtle)' }}
        >
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors whitespace-nowrap ${
                activeTab === tab.id ? 'bg-amber-500/20 text-amber-400' : ''
              }`}
              style={{
                color: activeTab === tab.id ? undefined : 'var(--color-text-secondary)',
              }}
              onMouseEnter={(e) => {
                if (activeTab !== tab.id) {
                  e.currentTarget.style.backgroundColor = 'var(--color-bg-secondary)';
                }
              }}
              onMouseLeave={(e) => {
                if (activeTab !== tab.id) {
                  e.currentTarget.style.backgroundColor = 'transparent';
                }
              }}
            >
              {tab.icon}
              {tab.label}
            </button>
          ))}
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-6">
          {activeTab === 'general' && (
            <GeneralTab
              config={config}
              updateConfig={updateConfig}
              showPasswords={showPasswords}
              setShowPasswords={setShowPasswords}
            />
          )}
          {activeTab === 'feeds' && (
            <FeedsTab
              config={config}
              updateConfig={updateConfig}
              showPasswords={showPasswords}
            />
          )}
          {activeTab === 'invoices' && (
            <InvoicesTab
              config={config}
              updateConfig={updateConfig}
              showPasswords={showPasswords}
              onUpload={() => fileInputRef.current?.click()}
              uploading={uploading}
            />
          )}
          {activeTab === 'mapping' && (
            <MappingTab config={config} updateConfig={updateConfig} />
          )}
          {activeTab === 'json' && (
            <JsonTab config={config} setConfig={setConfig} setHasChanges={setHasChanges} originalConfig={originalConfig} />
          )}
          {activeTab === 'history' && (
            <HistoryTab
              history={history}
              loading={historyLoading}
              selectedVersion={selectedHistoryVersion}
              historyConfig={historyConfig}
              onViewVersion={handleViewHistoryVersion}
              onRestore={handleRestore}
              onClose={() => {
                setSelectedHistoryVersion(null);
                setHistoryConfig(null);
              }}
            />
          )}
        </div>

        {/* Hidden file input */}
        <input
          ref={fileInputRef}
          type="file"
          accept=".csv,.pdf,.xlsx,.xls"
          onChange={handleFileUpload}
          className="hidden"
        />
      </div>
    </div>
  );
}

// -----------------------------------------------------------------------------
// Tab Components
// -----------------------------------------------------------------------------

interface TabProps {
  config: SupplierConfig;
  updateConfig: (updater: (cfg: SupplierConfig) => SupplierConfig) => void;
  showPasswords?: boolean;
  setShowPasswords?: (show: boolean) => void;
}

function GeneralTab({ config, updateConfig, showPasswords, setShowPasswords }: TabProps) {
  return (
    <div className="space-y-6 max-w-2xl">
      <div>
        <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
          Názov dodávateľa
        </label>
        <input
          type="text"
          value={config.name || ''}
          onChange={(e) =>
            updateConfig((cfg) => ({ ...cfg, name: e.target.value }))
          }
          className="w-full"
        />
      </div>

      <div className="flex items-center gap-3">
        <input
          type="checkbox"
          id="isActive"
          checked={config.is_active !== false}
          onChange={(e) =>
            updateConfig((cfg) => ({ ...cfg, is_active: e.target.checked }))
          }
        />
        <label htmlFor="isActive" style={{ color: 'var(--color-text-secondary)' }}>
          Aktívny dodávateľ
        </label>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
            Mena
          </label>
          <select
            value={config.adapter_settings?.currency || 'EUR'}
            onChange={(e) =>
              updateConfig((cfg) => ({
                ...cfg,
                adapter_settings: { ...cfg.adapter_settings, currency: e.target.value },
              }))
            }
            className="w-full"
          >
            <option value="EUR">EUR</option>
            <option value="CZK">CZK</option>
            <option value="USD">USD</option>
          </select>
        </div>
        <div>
          <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
            DPH sadzba (%)
          </label>
          <input
            type="number"
            value={config.adapter_settings?.vat_rate || 20}
            onChange={(e) =>
              updateConfig((cfg) => ({
                ...cfg,
                adapter_settings: { ...cfg.adapter_settings, vat_rate: parseInt(e.target.value) || 0 },
              }))
            }
            className="w-full"
          />
        </div>
      </div>

      <div>
        <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
          Prefix produktových kódov
        </label>
        <input
          type="text"
          value={config.adapter_settings?.mapping?.postprocess?.product_code_prefix || ''}
          onChange={(e) =>
            updateConfig((cfg) => ({
              ...cfg,
              adapter_settings: {
                ...cfg.adapter_settings,
                mapping: {
                  ...cfg.adapter_settings?.mapping,
                  postprocess: {
                    ...cfg.adapter_settings?.mapping?.postprocess,
                    product_code_prefix: e.target.value,
                  },
                },
              },
            }))
          }
          placeholder="napr. PL-"
          className="w-full"
        />
      </div>

      <div className="flex items-center gap-3 pt-4 border-t" style={{ borderColor: 'var(--color-border-subtle)' }}>
        <button
          onClick={() => setShowPasswords?.(!showPasswords)}
          className="flex items-center gap-2 text-sm"
          style={{ color: 'var(--color-text-tertiary)' }}
        >
          {showPasswords ? <EyeOff size={16} /> : <Eye size={16} />}
          {showPasswords ? 'Skryť heslá' : 'Zobraziť heslá'}
        </button>
      </div>
    </div>
  );
}

function FeedsTab({ config, updateConfig, showPasswords }: TabProps) {
  const currentKey = config.feeds?.current_key || 'products';
  const sources = config.feeds?.sources || {};
  const currentSource = sources[currentKey] || { mode: 'remote', remote: { url: '', auth: { mode: 'none' } } };

  const updateSource = (updates: any) => {
    updateConfig((cfg) => ({
      ...cfg,
      feeds: {
        ...cfg.feeds,
        sources: {
          ...cfg.feeds?.sources,
          [currentKey]: {
            ...currentSource,
            ...updates,
          },
        },
      },
    }));
  };

  return (
    <div className="space-y-6 max-w-2xl">
      <div>
        <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
          Aktívny feed
        </label>
        <select
          value={currentKey}
          onChange={(e) =>
            updateConfig((cfg) => ({ ...cfg, feeds: { ...cfg.feeds, current_key: e.target.value } }))
          }
          className="w-full"
        >
          {Object.keys(sources).map((key) => (
            <option key={key} value={key}>
              {key}
            </option>
          ))}
        </select>
      </div>

      <div>
        <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
          Režim
        </label>
        <select
          value={currentSource.mode || 'remote'}
          onChange={(e) => updateSource({ mode: e.target.value })}
          className="w-full"
        >
          <option value="remote">Remote (URL)</option>
          <option value="local">Lokálny súbor</option>
        </select>
      </div>

      {currentSource.mode === 'remote' && (
        <>
          <div>
            <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
              URL feedu
            </label>
            <input
              type="text"
              value={currentSource.remote?.url || ''}
              onChange={(e) =>
                updateSource({ remote: { ...currentSource.remote, url: e.target.value } })
              }
              placeholder="https://..."
              className="w-full font-mono text-sm"
            />
          </div>

          <div>
            <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
              Autentifikácia
            </label>
            <select
              value={currentSource.remote?.auth?.mode || 'none'}
              onChange={(e) =>
                updateSource({
                  remote: {
                    ...currentSource.remote,
                    auth: { ...currentSource.remote?.auth, mode: e.target.value },
                  },
                })
              }
              className="w-full"
            >
              <option value="none">Žiadna</option>
              <option value="basic">Basic Auth</option>
              <option value="bearer">Bearer Token</option>
              <option value="form">Form Login</option>
            </select>
          </div>

          {currentSource.remote?.auth?.mode === 'basic' && (
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
                  Username
                </label>
                <input
                  type="text"
                  value={currentSource.remote?.auth?.basic_user || ''}
                  onChange={(e) =>
                    updateSource({
                      remote: {
                        ...currentSource.remote,
                        auth: { ...currentSource.remote?.auth, basic_user: e.target.value },
                      },
                    })
                  }
                  className="w-full"
                />
              </div>
              <div>
                <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
                  Heslo
                </label>
                <input
                  type={showPasswords ? 'text' : 'password'}
                  value={currentSource.remote?.auth?.basic_pass || ''}
                  onChange={(e) =>
                    updateSource({
                      remote: {
                        ...currentSource.remote,
                        auth: { ...currentSource.remote?.auth, basic_pass: e.target.value },
                      },
                    })
                  }
                  className="w-full"
                />
              </div>
            </div>
          )}

          {currentSource.remote?.auth?.mode === 'bearer' && (
            <div>
              <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
                Token
              </label>
              <input
                type={showPasswords ? 'text' : 'password'}
                value={currentSource.remote?.auth?.token || ''}
                onChange={(e) =>
                  updateSource({
                    remote: {
                      ...currentSource.remote,
                      auth: { ...currentSource.remote?.auth, token: e.target.value },
                    },
                  })
                }
                className="w-full font-mono text-sm"
              />
            </div>
          )}
        </>
      )}

      {currentSource.mode === 'local' && (
        <div>
          <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
            Cesta k súboru
          </label>
          <input
            type="text"
            value={currentSource.local_path || ''}
            onChange={(e) => updateSource({ local_path: e.target.value })}
            placeholder="/path/to/feed.xml"
            className="w-full font-mono text-sm"
          />
        </div>
      )}
    </div>
  );
}

function InvoicesTab({ config, updateConfig, showPasswords, onUpload, uploading }: TabProps & { onUpload: () => void; uploading: boolean }) {
  const download = config.invoices?.download || { strategy: 'manual', web: { login: {} } };
  const webLogin = download.web?.login || {};

  const updateDownload = (updates: any) => {
    updateConfig((cfg) => ({
      ...cfg,
      invoices: {
        ...cfg.invoices,
        download: {
          ...cfg.invoices?.download,
          ...updates,
        },
      },
    }));
  };

  const updateWebLogin = (updates: any) => {
    updateConfig((cfg) => ({
      ...cfg,
      invoices: {
        ...cfg.invoices,
        download: {
          ...cfg.invoices?.download,
          web: {
            ...cfg.invoices?.download?.web,
            login: {
              ...cfg.invoices?.download?.web?.login,
              ...updates,
            },
          },
        },
      },
    }));
  };

  return (
    <div className="space-y-6 max-w-2xl">
      <div className="flex items-center justify-between">
        <h3 className="font-medium" style={{ color: 'var(--color-text-primary)' }}>
          Nastavenie faktúr
        </h3>
        <Button
          variant="secondary"
          size="sm"
          onClick={onUpload}
          loading={uploading}
          icon={<Upload size={16} />}
        >
          Nahrať faktúru
        </Button>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
            Štruktúra priečinkov
          </label>
          <select
            value={config.invoices?.layout || 'flat'}
            onChange={(e) =>
              updateConfig((cfg) => ({
                ...cfg,
                invoices: { ...cfg.invoices, layout: e.target.value as 'flat' | 'yearly' },
              }))
            }
            className="w-full"
          >
            <option value="flat">Plochá (všetky v jednom)</option>
            <option value="yearly">Podľa rokov</option>
          </select>
        </div>
        <div>
          <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
            Mesiace dozadu
          </label>
          <input
            type="number"
            value={config.invoices?.months_back_default || 3}
            onChange={(e) =>
              updateConfig((cfg) => ({
                ...cfg,
                invoices: { ...cfg.invoices, months_back_default: parseInt(e.target.value) || 3 },
              }))
            }
            className="w-full"
          />
        </div>
      </div>

      <div>
        <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
          Stratégia sťahovania
        </label>
        <select
          value={download.strategy || 'manual'}
          onChange={(e) => updateDownload({ strategy: e.target.value })}
          className="w-full"
        >
          <option value="manual">Manuálny upload</option>
          <option value="paul-lange-web">Paul-Lange Web (scraping)</option>
          <option value="web">Vlastný web scraping</option>
          <option value="api">API</option>
          <option value="disabled">Vypnuté</option>
        </select>
      </div>

      {(download.strategy === 'web' || download.strategy === 'paul-lange-web') && (
        <div
          className="space-y-4 p-4 rounded-lg border"
          style={{ borderColor: 'var(--color-border-subtle)', backgroundColor: 'var(--color-bg-secondary)' }}
        >
          <h4 className="font-medium text-sm flex items-center gap-2" style={{ color: 'var(--color-text-primary)' }}>
            <Key size={16} />
            Prihlasovacie údaje
          </h4>

          <div>
            <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
              Login URL
            </label>
            <input
              type="text"
              value={webLogin.login_url || ''}
              onChange={(e) => updateWebLogin({ login_url: e.target.value })}
              placeholder="https://..."
              className="w-full font-mono text-sm"
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
                Používateľské meno
              </label>
              <input
                type="text"
                value={webLogin.username || ''}
                onChange={(e) => updateWebLogin({ username: e.target.value })}
                className="w-full"
              />
            </div>
            <div>
              <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
                Heslo
              </label>
              <input
                type={showPasswords ? 'text' : 'password'}
                value={webLogin.password || ''}
                onChange={(e) => updateWebLogin({ password: e.target.value })}
                className="w-full"
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
                Názov poľa pre login
              </label>
              <input
                type="text"
                value={webLogin.user_field || 'login'}
                onChange={(e) => updateWebLogin({ user_field: e.target.value })}
                className="w-full"
              />
            </div>
            <div>
              <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
                Názov poľa pre heslo
              </label>
              <input
                type="text"
                value={webLogin.pass_field || 'password'}
                onChange={(e) => updateWebLogin({ pass_field: e.target.value })}
                className="w-full"
              />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function MappingTab({ config, updateConfig }: TabProps) {
  const mapping = config.adapter_settings?.mapping || {};
  const invoiceToCanon = mapping.invoice_to_canon || {};

  const updateInvoiceToCanon = (field: string, value: string | null) => {
    updateConfig((cfg) => ({
      ...cfg,
      adapter_settings: {
        ...cfg.adapter_settings,
        mapping: {
          ...cfg.adapter_settings?.mapping,
          invoice_to_canon: {
            ...cfg.adapter_settings?.mapping?.invoice_to_canon,
            [field]: value,
          },
        },
      },
    }));
  };

  const fields = [
    { key: 'SCM', label: 'Kód produktu (SKU)' },
    { key: 'EAN', label: 'EAN / Čiarový kód' },
    { key: 'TITLE', label: 'Názov produktu' },
    { key: 'QTY', label: 'Množstvo' },
    { key: 'UNIT_PRICE_EX', label: 'Cena bez DPH' },
    { key: 'UNIT_PRICE_INC', label: 'Cena s DPH' },
  ];

  return (
    <div className="space-y-6 max-w-2xl">
      <div>
        <h3 className="font-medium mb-4" style={{ color: 'var(--color-text-primary)' }}>
          Mapovanie stĺpcov faktúry
        </h3>
        <p className="text-sm mb-4" style={{ color: 'var(--color-text-tertiary)' }}>
          Zadajte názvy stĺpcov vo faktúrach od tohto dodávateľa. Systém ich použije na automatické
          rozpoznanie údajov.
        </p>

        <div className="space-y-3">
          {fields.map(({ key, label }) => (
            <div key={key} className="grid grid-cols-2 gap-4 items-center">
              <label className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
                {label}
              </label>
              <input
                type="text"
                value={invoiceToCanon[key] || ''}
                onChange={(e) => updateInvoiceToCanon(key, e.target.value || null)}
                placeholder={`Názov stĺpca pre ${key}`}
                className="w-full text-sm"
              />
            </div>
          ))}
        </div>
      </div>

      <div
        className="p-4 rounded-lg border"
        style={{ borderColor: 'var(--color-border-subtle)', backgroundColor: 'var(--color-bg-secondary)' }}
      >
        <h4 className="font-medium text-sm mb-2" style={{ color: 'var(--color-text-primary)' }}>
          Postprocess nastavenia
        </h4>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm mb-1.5" style={{ color: 'var(--color-text-secondary)' }}>
              Zdroj ceny
            </label>
            <select
              value={mapping.postprocess?.unit_price_source || 'ex'}
              onChange={(e) =>
                updateConfig((cfg) => ({
                  ...cfg,
                  adapter_settings: {
                    ...cfg.adapter_settings,
                    mapping: {
                      ...cfg.adapter_settings?.mapping,
                      postprocess: {
                        ...cfg.adapter_settings?.mapping?.postprocess,
                        unit_price_source: e.target.value,
                      },
                    },
                  },
                }))
              }
              className="w-full"
            >
              <option value="ex">Cena bez DPH</option>
              <option value="inc">Cena s DPH</option>
            </select>
          </div>
        </div>
      </div>
    </div>
  );
}

function JsonTab({
  config,
  setConfig,
  setHasChanges,
  originalConfig,
}: {
  config: SupplierConfig;
  setConfig: (cfg: SupplierConfig) => void;
  setHasChanges: (has: boolean) => void;
  originalConfig: SupplierConfig | null;
}) {
  const [jsonText, setJsonText] = useState(() => JSON.stringify(config, null, 2));
  const [parseError, setParseError] = useState<string | null>(null);

  useEffect(() => {
    setJsonText(JSON.stringify(config, null, 2));
  }, [config]);

  const handleJsonChange = (text: string) => {
    setJsonText(text);
    try {
      const parsed = JSON.parse(text);
      setParseError(null);
      setConfig(parsed);
      setHasChanges(JSON.stringify(parsed) !== JSON.stringify(originalConfig));
    } catch (e: any) {
      setParseError(e.message);
    }
  };

  const handleFormat = () => {
    try {
      const parsed = JSON.parse(jsonText);
      setJsonText(JSON.stringify(parsed, null, 2));
      setParseError(null);
    } catch (e: any) {
      setParseError(e.message);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Code size={16} style={{ color: 'var(--color-text-tertiary)' }} />
          <span className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
            JSON konfigurácia
          </span>
        </div>
        <div className="flex items-center gap-2">
          {parseError && (
            <span className="text-xs text-red-400 flex items-center gap-1">
              <AlertCircle size={14} />
              Neplatný JSON
            </span>
          )}
          <Button variant="secondary" size="sm" onClick={handleFormat}>
            Formátovať
          </Button>
        </div>
      </div>

      <div
        className="rounded-lg border overflow-hidden"
        style={{ borderColor: parseError ? 'var(--color-error)' : 'var(--color-border-subtle)' }}
      >
        <textarea
          value={jsonText}
          onChange={(e) => handleJsonChange(e.target.value)}
          className="w-full h-[500px] p-4 font-mono text-sm resize-none"
          style={{
            backgroundColor: 'var(--color-bg-primary)',
            color: 'var(--color-text-primary)',
            border: 'none',
            outline: 'none',
          }}
          spellCheck={false}
        />
      </div>

      <p className="text-xs flex items-center gap-2" style={{ color: 'var(--color-text-tertiary)' }}>
        <AlertTriangle size={14} />
        Heslá sú zobrazené - dajte pozor pri zdieľaní screenshotov
      </p>
    </div>
  );
}

function HistoryTab({
  history,
  loading,
  selectedVersion,
  historyConfig,
  onViewVersion,
  onRestore,
  onClose,
}: {
  history: SupplierHistoryEntry[];
  loading: boolean;
  selectedVersion: string | null;
  historyConfig: SupplierConfig | null;
  onViewVersion: (version: string) => void;
  onRestore: (version: string) => void;
  onClose: () => void;
}) {
  if (loading) {
    return (
      <div className="flex items-center justify-center py-12" style={{ color: 'var(--color-text-tertiary)' }}>
        Načítavam históriu...
      </div>
    );
  }

  if (selectedVersion && historyConfig) {
    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <button onClick={onClose} className="text-sm" style={{ color: 'var(--color-accent)' }}>
              ← Späť na zoznam
            </button>
            <span style={{ color: 'var(--color-text-tertiary)' }}>•</span>
            <span className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
              Verzia: {selectedVersion}
            </span>
          </div>
          <Button variant="secondary" size="sm" onClick={() => onRestore(selectedVersion)} icon={<RotateCcw size={16} />}>
            Obnoviť túto verziu
          </Button>
        </div>

        <div
          className="rounded-lg border overflow-hidden"
          style={{ borderColor: 'var(--color-border-subtle)' }}
        >
          <pre
            className="p-4 text-sm overflow-auto max-h-[500px]"
            style={{
              backgroundColor: 'var(--color-bg-primary)',
              color: 'var(--color-text-secondary)',
              fontFamily: 'var(--font-mono)',
            }}
          >
            {JSON.stringify(historyConfig, null, 2)}
          </pre>
        </div>
      </div>
    );
  }

  if (history.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-12" style={{ color: 'var(--color-text-tertiary)' }}>
        <History size={32} className="mb-2 opacity-50" />
        <p>Zatiaľ žiadna história</p>
        <p className="text-xs mt-1">História sa vytvorí po prvom uložení zmien</p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <p className="text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
        Posledných {history.length} verzií konfigurácie
      </p>

      {history.map((entry, index) => (
        <div
          key={entry.version}
          className="flex items-center justify-between p-4 rounded-lg border"
          style={{
            borderColor: 'var(--color-border-subtle)',
            backgroundColor: index === 0 ? 'var(--color-bg-tertiary)' : 'var(--color-bg-secondary)',
          }}
        >
          <div>
            <div className="flex items-center gap-2">
              {index === 0 && (
                <span className="text-xs px-2 py-0.5 rounded-full bg-green-500/20 text-green-400">
                  Najnovšia
                </span>
              )}
              <span className="text-sm" style={{ color: 'var(--color-text-primary)' }}>
                {new Date(entry.timestamp).toLocaleString('sk-SK')}
              </span>
            </div>
            <p className="text-xs mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
              {(entry.size_bytes / 1024).toFixed(1)} KB
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={() => onViewVersion(entry.version)}>
              Zobraziť
            </Button>
            <Button variant="secondary" size="sm" onClick={() => onRestore(entry.version)} icon={<RotateCcw size={14} />}>
              Obnoviť
            </Button>
          </div>
        </div>
      ))}
    </div>
  );
}

// -----------------------------------------------------------------------------
// Main Page Component
// -----------------------------------------------------------------------------
export function SuppliersPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  
  const [suppliers, setSuppliers] = useState<SupplierSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  
  // Editor state from URL
  const editingCode = searchParams.get('edit');

  const loadSuppliers = async () => {
    setLoading(true);
    setError(null);
    try {
      const list = await listSuppliers();
      setSuppliers(list);
    } catch (err: any) {
      setError(err.message || 'Nepodarilo sa načítať dodávateľov');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadSuppliers();
  }, []);

  const handleEdit = (code: string) => {
    setSearchParams({ edit: code });
  };

  const handleCloseEditor = () => {
    setSearchParams({});
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1
            className="text-2xl font-semibold"
            style={{ fontFamily: 'var(--font-display)', color: 'var(--color-text-primary)' }}
          >
            Dodávatelia
          </h1>
          <p className="text-sm mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
            Správa dodávateľov a ich konfigurácií
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="secondary" onClick={loadSuppliers} icon={<RefreshCw size={16} />}>
            Obnoviť
          </Button>
          <Button onClick={() => setShowCreateModal(true)} icon={<Plus size={16} />}>
            Nový dodávateľ
          </Button>
        </div>
      </div>

      {/* Error */}
      {error && (
        <div className="p-4 rounded-lg bg-red-500/10 border border-red-500/30 text-red-400">
          {error}
        </div>
      )}

      {/* Loading */}
      {loading && (
        <div className="flex items-center justify-center py-12" style={{ color: 'var(--color-text-tertiary)' }}>
          Načítavam...
        </div>
      )}

      {/* Supplier list */}
      {!loading && suppliers.length === 0 && (
        <div
          className="rounded-xl border p-12 text-center"
          style={{
            backgroundColor: 'var(--color-bg-secondary)',
            borderColor: 'var(--color-border-subtle)',
          }}
        >
          <Package size={48} className="mx-auto mb-4" style={{ color: 'var(--color-text-tertiary)' }} />
          <h2 className="text-lg font-medium mb-2" style={{ color: 'var(--color-text-primary)' }}>
            Zatiaľ žiadni dodávatelia
          </h2>
          <p className="text-sm mb-6" style={{ color: 'var(--color-text-tertiary)' }}>
            Pridajte prvého dodávateľa pre začatie práce s faktúrami
          </p>
          <Button onClick={() => setShowCreateModal(true)} icon={<Plus size={16} />}>
            Pridať dodávateľa
          </Button>
        </div>
      )}

      {!loading && suppliers.length > 0 && (
        <div className="grid gap-4">
          {suppliers.map((supplier) => (
            <SupplierCard key={supplier.code} supplier={supplier} onEdit={handleEdit} />
          ))}
        </div>
      )}

      {/* Create modal */}
      <CreateSupplierModal
        open={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        onCreated={loadSuppliers}
      />

      {/* Editor */}
      {editingCode && (
        <SupplierEditor
          code={editingCode}
          onClose={handleCloseEditor}
          onSaved={loadSuppliers}
        />
      )}
    </div>
  );
}

export default SuppliersPage;
