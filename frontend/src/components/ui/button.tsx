import * as React from "react";

type BaseProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  className?: string;
};

// Minimal Button without external deps
export const Button = React.forwardRef<HTMLButtonElement, BaseProps>(
  ({ className = "", ...props }, ref) => {
    return (
      <button
        ref={ref}
        className={
          "inline-flex items-center justify-center rounded-md px-4 py-2 text-sm font-medium " +
          "bg-neutral-800 text-white hover:bg-neutral-700 transition focus:outline-none " +
          "focus-visible:ring-1 disabled:opacity-50 disabled:pointer-events-none " + className
        }
        {...props}
      />
    );
  }
);
Button.displayName = "Button";

export function PrimaryButton({ className = "", ...props }: BaseProps) {
  return <Button className={"rounded-2xl " + className} {...props} />;
}

export function OutlineButton({ className = "", ...props }: BaseProps) {
  return (
    <Button
      className={
        "rounded-2xl border border-neutral-700 bg-transparent hover:bg-neutral-900 " + className
      }
      {...props}
    />
  );
}

export function Pill({
  className = "",
  ...props
}: React.HTMLAttributes<HTMLSpanElement>) {
  return (
    <span
      className={
        "inline-flex items-center rounded-full border border-neutral-700 bg-neutral-900 " +
        "px-2.5 py-0.5 text-xs text-neutral-300 " + className
      }
      {...props}
    />
  );
}
