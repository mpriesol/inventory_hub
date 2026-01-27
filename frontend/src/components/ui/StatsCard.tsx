import React from 'react';

type StatsCardVariant = 'default' | 'warning' | 'success' | 'error' | 'info';

interface StatsCardProps {
  icon: React.ReactNode;
  value: string | number;
  label: string;
  sublabel?: string;
  variant?: StatsCardVariant;
  onClick?: () => void;
  className?: string;
}

const variantStyles: Record<StatsCardVariant, { border: string; bg: string; badge?: string }> = {
  default: {
    border: 'var(--color-border-subtle)',
    bg: 'var(--color-bg-secondary)',
  },
  warning: {
    border: 'rgba(234, 179, 8, 0.3)',
    bg: 'rgba(234, 179, 8, 0.05)',
    badge: 'Attention',
  },
  success: {
    border: 'rgba(34, 197, 94, 0.3)',
    bg: 'rgba(34, 197, 94, 0.05)',
  },
  error: {
    border: 'rgba(239, 68, 68, 0.3)',
    bg: 'rgba(239, 68, 68, 0.05)',
    badge: 'Critical',
  },
  info: {
    border: 'rgba(59, 130, 246, 0.3)',
    bg: 'rgba(59, 130, 246, 0.05)',
  },
};

export function StatsCard({
  icon,
  value,
  label,
  sublabel,
  variant = 'default',
  onClick,
  className = '',
}: StatsCardProps) {
  const styles = variantStyles[variant];
  const isClickable = !!onClick;

  return (
    <div
      onClick={onClick}
      className={`p-4 rounded-xl transition-all ${className}`}
      style={{
        backgroundColor: styles.bg,
        border: `1px solid ${styles.border}`,
        cursor: isClickable ? 'pointer' : 'default',
      }}
      onMouseEnter={(e) => {
        if (isClickable) {
          e.currentTarget.style.borderColor = variant === 'default' 
            ? 'var(--color-border-strong)' 
            : styles.border.replace('0.3', '0.5');
        }
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = styles.border;
      }}
    >
      <div className="flex items-start justify-between">
        <span className="text-2xl">{icon}</span>
        {styles.badge && (
          <span
            className="text-xs px-2 py-0.5 rounded-full"
            style={{
              backgroundColor: variant === 'warning' 
                ? 'var(--color-warning-muted)' 
                : 'var(--color-error-muted)',
              color: variant === 'warning' 
                ? 'var(--color-warning)' 
                : 'var(--color-error)',
            }}
          >
            {styles.badge}
          </span>
        )}
      </div>
      <div className="mt-3">
        <div
          className="text-2xl font-semibold"
          style={{
            fontFamily: 'var(--font-display)',
            color: 'var(--color-text-primary)',
          }}
        >
          {value}
        </div>
        <div
          className="text-sm mt-0.5"
          style={{ color: 'var(--color-text-secondary)' }}
        >
          {label}
        </div>
        {sublabel && (
          <div
            className="text-xs mt-1"
            style={{ color: 'var(--color-text-tertiary)' }}
          >
            {sublabel}
          </div>
        )}
      </div>
    </div>
  );
}

export default StatsCard;
