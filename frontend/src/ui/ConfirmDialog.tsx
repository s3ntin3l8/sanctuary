import { Button } from './Button'
import { Modal } from './Modal'

type Props = {
  open: boolean
  onClose: () => void
  onConfirm: () => void
  title: string
  body: string
  label: string
  danger?: boolean
  pending?: boolean
}

export function ConfirmDialog({
  open,
  onClose,
  onConfirm,
  title,
  body,
  label,
  danger,
  pending,
}: Props) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      icon={danger ? 'warning' : 'help'}
      width={420}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button variant={danger ? 'danger' : 'primary'} onClick={onConfirm} disabled={pending}>
            {label}
          </Button>
        </>
      }
    >
      <p className="text-[12.5px] leading-relaxed text-ink2">{body}</p>
    </Modal>
  )
}
