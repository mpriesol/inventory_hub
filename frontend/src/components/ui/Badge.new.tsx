import React from 'react';

type BadgeVariant = 'default' | 'success' | 'warning' | 'error' | 'info' | 'accent';
type BadgeSize = 'sm' | 'md';

interface BadgeProps {
  children: React.ReactNode;
  variant?: BadgeVariant;
  size?: BadgeSize;
  className?: string;
}

const variantStyles: Record<BadgeVariant, { bg: string; color: string }> = {
  default: {
    bg: 'var(--color-bg-tertiary)',
    color: 'var(--color-text-secondary)',
  },
  success: {
    bg: 'var(--color-success-muted)',
    color: 'var(--color-success)',
  },
  warning: {
    bg: 'var(--color-warning-muted)',
    color: 'var(--color-warning)',
  },
  error: {
    bg: 'var(--color-error-muted)',
    color: 'var(--color-error)',
  },
  info: {
    bg: 'var(--color-info-muted)',
    color: 'var(--color-info)',
  },
  accent: {
    bg: 'var(--color-accent-muted)',
    color: 'var(--color-accent)',
  },
};

const sizeStyles: Record<BadgeSize, string> = {
  sm: 'text-xs px-1.5 py-0.5',
  md: 'text-xs px-2 py-1',
};

export function Badge({
  children,
  variant = 'default',
  size = 'sm',
  className = '',
}: BadgeProps) {
  const styles = variantStyles[variant];

  return (
    <span
      className={`inline-flex items-center rounded-full font-medium ${sizeStyles[size]} ${className}`}
      style={{
        backgroundColor: styles.bg,
        color: styles.color,
      }}
    >
      {children}
    </span>
  );
}

// Status-specific badges for invoices/receiving
type StatusType = 'pending' | 'partial' | 'complete' | 'error';

interface StatusBadgeProps {
  status: StatusType;
  label?: string;
  className?: string;
}

const statusConfig: Record<StatusType, { icon: string; label: string; variant: BadgeVariant }> = {
  pending: { icon: '○', label: 'Pending', variant: 'warning' },
  partial: { icon: '◐', label: 'Partial', variant: 'info' },
  complete: { icon: '✓', label: 'Complete', variant: 'success' },
  error: { icon: '!', label: 'Error', variant: 'error' },
};

export function StatusBadge({ status, label, className = '' }: StatusBadgeProps) {
  const config = statusConfig[status];
  
  return (
    <Badge variant={config.variant} className={className}>
      {config.icon} {label || config.label}
    </Badge>
  );
}

export default Badge;
