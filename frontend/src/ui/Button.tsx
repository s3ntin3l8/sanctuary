import type { ButtonHTMLAttributes } from 'react'

const base =
  'inline-flex items-center justify-center gap-2 rounded-[9px] px-4 py-2.5 text-[13px] font-semibold ' +
  'transition-opacity focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent ' +
  'disabled:cursor-not-allowed disabled:opacity-50'

const variants = {
  primary: 'bg-accent text-on-accent hover:opacity-90',
  secondary: 'border border-line bg-panel2 text-ink hover:border-accent',
}

export function buttonClass(variant: keyof typeof variants = 'primary', className = '') {
  return `${base} ${variants[variant]} ${className}`
}

type Props = ButtonHTMLAttributes<HTMLButtonElement> & { variant?: keyof typeof variants }

export function Button({ variant = 'primary', className, type = 'button', ...rest }: Props) {
  return <button type={type} className={buttonClass(variant, className)} {...rest} />
}
