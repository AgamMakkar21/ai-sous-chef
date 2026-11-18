import type { ComponentPropsWithRef, ReactNode } from "react";

type CartoonButtonProps = Omit<
  ComponentPropsWithRef<"button">,
  "children" | "color"
> &
  (
    | {
        label: ReactNode;
        children?: never;
      }
    | {
        label?: never;
        children: ReactNode;
      }
  ) & {
    color?: string;
    hasHighlight?: boolean;
  };

function CartoonContent({
  children,
  hasHighlight,
}: {
  children: ReactNode;
  hasHighlight: boolean;
}) {
  return (
    <>
      <span className="cartoon-label">{children}</span>
      {hasHighlight && (
        <span className="cartoon-highlight" aria-hidden="true" />
      )}
    </>
  );
}

export function CartoonButton({
  label,
  children,
  color = "bg-[var(--cp-accent)]",
  hasHighlight = true,
  disabled = false,
  type = "button",
  className = "",
  ...props
}: CartoonButtonProps) {
  return (
    <button
      {...props}
      type={type}
      disabled={disabled}
      className={`button cartoon-button ${color} ${className}`}
    >
      <CartoonContent hasHighlight={hasHighlight && !disabled}>
        {label ?? children}
      </CartoonContent>
    </button>
  );
}

export function CartoonLink({
  children,
  className = "",
  ...props
}: ComponentPropsWithRef<"a">) {
  return (
    <a {...props} className={`button cartoon-button ${className}`}>
      <CartoonContent
        hasHighlight={
          props["aria-disabled"] !== true && props["aria-disabled"] !== "true"
        }
      >
        {children}
      </CartoonContent>
    </a>
  );
}
