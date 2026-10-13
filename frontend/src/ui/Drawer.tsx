import { type ReactNode, useRef } from 'react'

import { Icon } from './Icon'
import { useOverlay } from './useOverlay'

export function Drawer({
  open,
  onClose,
  title,
  header = true,
  inline = false,
  inlineWidth = 400,
  keepMounted = false,
  children,
}: {
  open: boolean
  onClose: () => void
  title: string
  header?: boolean
  inline?: boolean
  inlineWidth?: number
  keepMounted?: boolean
  children: ReactNode
}) {
  const panel = useRef<HTMLDivElement>(null)
  useOverlay(open && !inline, panel, onClose)
  if (!open && !inline && !keepMounted) return null
  // Keep the same subtree across breakpoints, including chat drafts and conversation state.
  return (
    <div
      hidden={!open && !inline}
      className={
        inline
          ? 'contents'
          : open
            ? 'fixed inset-0 z-130 flex justify-end bg-[rgba(6,14,32,.62)]'
            : 'hidden'
      }
      onMouseDown={(event) => {
        if (!inline && event.target === event.currentTarget) onClose()
      }}
    >
      <div
        ref={panel}
        role={!inline && open ? 'dialog' : undefined}
        aria-modal={inline ? undefined : true}
        aria-label={inline ? undefined : title}
        tabIndex={inline ? undefined : -1}
        style={inline ? { width: inlineWidth } : undefined}
        className={`flex h-full shrink-0 flex-col ${inline ? '' : 'w-full max-w-[400px] border-l border-line bg-panel shadow-xl'}`}
      >
        {header && !inline && (
          <header className="flex shrink-0 items-center gap-3 border-b border-line px-4 py-3">
            <h2 className="min-w-0 flex-1 truncate font-display text-[14px] font-bold">{title}</h2>
            <button
              type="button"
              aria-label={`Close ${title}`}
              onClick={onClose}
              className="rounded-md p-1 text-muted hover:text-ink"
            >
              <Icon name="close" size={18} />
            </button>
          </header>
        )}
        <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
      </div>
    </div>
  )
}
