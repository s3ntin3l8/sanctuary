type Props = { name: string; size?: number; filled?: boolean; className?: string }

/** A Material Symbols Outlined glyph, sized in pixels. Decorative by default. */
export function Icon({ name, size = 18, filled = false, className = '' }: Props) {
  return (
    <span
      aria-hidden
      className={`icon ${filled ? 'icon-filled' : ''} ${className}`}
      style={{ fontSize: size }}
    >
      {name}
    </span>
  )
}
