import type { ButtonHTMLAttributes } from 'react'

const base =
  'inline-flex items-center justify-center gap-2 font-semibold ' +
  'transition-opacity focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent ' +
  'disabled:cursor-not-allowed disabled:opacity-50'

const variants = {
  primary: 'bg-accent text-on-accent hover:opacity-90',
  secondary: 'border border-line bg-panel2 text-ink hover:border-accent',
  danger: 'bg-danger text-white hover:opacity-90',
  'danger-outline': 'border border-danger/40 bg-panel2 text-danger hover:border-danger',
}

const sizes = {
  sm: 'rounded-md px-2.5 py-1 text-[10.5px]',
  md: 'rounded-lg px-3 py-1.5 text-[11.5px]',
  lg: 'rounded-[9px] px-4 py-2.5 text-[13px]',
  icon: 'h-7 w-7 rounded-[7px] p-0 text-[11.5px]',
}

export type ButtonVariant = keyof typeof variants
export type ButtonSize = keyof typeof sizes

/** Class string for `<Button>` and for links styled as buttons. `className` is for layout only. */
export function buttonClass(
  variant: ButtonVariant = 'primary',
  className = '',
  size: ButtonSize = 'md',
) {
  return `${base} ${sizes[size]} ${variants[variant]} ${className}`
}

type Props = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant
  size?: ButtonSize
}

export function Button({
  variant = 'primary',
  size = 'md',
  className,
  type = 'button',
  ...rest
}: Props) {
  return <button type={type} className={buttonClass(variant, className, size)} {...rest} />
}
