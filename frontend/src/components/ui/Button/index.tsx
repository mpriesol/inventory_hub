import * as React from "react";
import { Button as ShadcnButton } from "../button";

type BtnProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  className?: string;
  children?: React.ReactNode;
};

/** Primary filled button (rounded) — wraps shadcn <Button> */
export function PrimaryButton({ className = "", ...props }: BtnProps) {
  return <ShadcnButton className={`rounded-2xl ${className}`} {...props} />;
}

/** Outline button (rounded) — wraps shadcn <Button variant="outline"> */
export function OutlineButton({ className = "", ...props }: BtnProps) {
  return <ShadcnButton variant="outline" className={`rounded-2xl ${className}`} {...props} />;
}

/** Small pill label used in tables/metadata */
export function Pill({
  className = "",
  ...props
}: React.HTMLAttributes<HTMLSpanElement>) {
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs text-neutral-300 border-neutral-700 bg-neutral-900 ${className}`}
      {...props}
    />
  );
}
