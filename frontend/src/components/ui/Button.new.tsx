import React from 'react';

type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'success';
type ButtonSize = 'sm' | 'md' | 'lg';

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  children: React.ReactNode;
  loading?: boolean;
  icon?: React.ReactNode;
  iconPosition?: 'left' | 'right';
}

const variantStyles: Record<ButtonVariant, {
  bg: string;
  bgHover: string;
  color: string;
  border: string;
}> = {
  primary: {
    bg: 'var(--color-accent)',
    bgHover: 'var(--color-accent-hover)',
    color: 'var(--color-text-inverse)',
    border: 'transparent',
  },
  secondary: {
    bg: 'transparent',
    bgHover: 'var(--color-bg-secondary)',
    color: 'var(--color-text-secondary)',
    border: 'var(--color-border-subtle)',
  },
  ghost: {
    bg: 'transparent',
    bgHover: 'var(--color-bg-secondary)',
    color: 'var(--color-text-secondary)',
    border: 'transparent',
  },
  danger: {
    bg: 'transparent',
    bgHover: 'var(--color-error-subtle)',
    color: 'var(--color-error)',
    border: 'rgba(239, 68, 68, 0.3)',
  },
  success: {
    bg: 'var(--color-success)',
    bgHover: '#16a34a',
    color: 'var(--color-text-inverse)',
    border: 'transparent',
  },
};

const sizeStyles: Record<ButtonSize, { padding: string; fontSize: string; height: string }> = {
  sm: { padding: '0.375rem 0.75rem', fontSize: 'var(--text-xs)', height: '2rem' },
  md: { padding: '0.5rem 1rem', fontSize: 'var(--text-sm)', height: '2.5rem' },
  lg: { padding: '0.75rem 1.5rem', fontSize: 'var(--text-sm)', height: '3rem' },
};

export function Button({
  variant = 'primary',
  size = 'md',
  children,
  loading = false,
  icon,
  iconPosition = 'left',
  disabled,
  className = '',
  style,
  ...props
}: ButtonProps) {
  const variantStyle = variantStyles[variant];
  const sizeStyle = sizeStyles[size];
  const isDisabled = disabled || loading;

  return (
    <button
      disabled={isDisabled}
      className={`
        inline-flex items-center justify-center gap-2 rounded-lg font-medium
        transition-all
        ${isDisabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}
        ${className}
      `}
      style={{
        backgroundColor: variantStyle.bg,
        color: variantStyle.color,
        border: `1px solid ${variantStyle.border}`,
        padding: sizeStyle.padding,
        fontSize: sizeStyle.fontSize,
        height: sizeStyle.height,
        ...style,
      }}
      onMouseEnter={(e) => {
        if (!isDisabled) {
          e.currentTarget.style.backgroundColor = variantStyle.bgHover;
        }
      }}
      onMouseLeave={(e) => {
        if (!isDisabled) {
          e.currentTarget.style.backgroundColor = variantStyle.bg;
        }
      }}
      {...props}
    >
      {loading && (
        <svg
          className="animate-spin h-4 w-4"
          fill="none"
          viewBox="0 0 24 24"
        >
          <circle
            className="opacity-25"
            cx="12"
            cy="12"
            r="10"
            stroke="currentColor"
            strokeWidth="4"
          />
          <path
            className="opacity-75"
            fill="currentColor"
            d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
          />
        </svg>
      )}
      {icon && iconPosition === 'left' && !loading && icon}
      {children}
      {icon && iconPosition === 'right' && icon}
    </button>
  );
}

// Quick Action Button (for dashboard)
interface QuickActionProps {
  icon: React.ReactNode;
  label: string;
  description?: string;
  onClick?: () => void;
}

export function QuickAction({ icon, label, description, onClick }: QuickActionProps) {
  return (
    <button
      onClick={onClick}
      className="flex flex-col items-center p-4 rounded-xl border transition-all text-left group"
      style={{
        backgroundColor: 'var(--color-bg-secondary)',
        borderColor: 'var(--color-border-subtle)',
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = 'var(--color-border-accent)';
        e.currentTarget.style.backgroundColor = 'var(--color-accent-subtle)';
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = 'var(--color-border-subtle)';
        e.currentTarget.style.backgroundColor = 'var(--color-bg-secondary)';
      }}
    >
      <div
        className="w-12 h-12 rounded-full flex items-center justify-center transition-colors"
        style={{
          backgroundColor: 'var(--color-accent-subtle)',
          color: 'var(--color-accent)',
        }}
      >
        {icon}
      </div>
      <div
        className="mt-3 text-sm font-medium"
        style={{ color: 'var(--color-text-primary)' }}
      >
        {label}
      </div>
      {description && (
        <div
          className="text-xs mt-1"
          style={{ color: 'var(--color-text-tertiary)' }}
        >
          {description}
        </div>
      )}
    </button>
  );
}

export default Button;
