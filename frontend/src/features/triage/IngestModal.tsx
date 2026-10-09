import { type DragEvent, useRef, useState } from 'react'

import type { Schemas } from '../../api/client'
import { useDocumentStatus, useUpload, useUploadTarget } from '../../api/triage'
import { Badge } from '../../ui/Badge'
import { Button } from '../../ui/Button'
import { Field, inputClass } from '../../ui/Field'
import { Icon } from '../../ui/Icon'
import { Modal } from '../../ui/Modal'

const ACCEPT = '.pdf,.docx,.txt,.md,.pptx,.xlsx,.eml'

type Props = { open: boolean; onClose: () => void; caseId: string | null }

/** Drop files, queue them, and watch each one move through the pipeline. */
export function IngestModal({ open, onClose, caseId }: Props) {
  const [files, setFiles] = useState<File[]>([])
  const [results, setResults] = useState<Schemas['UploadResult'][] | null>(null)
  const [dragging, setDragging] = useState(false)
  const [parentId, setParentId] = useState<number | null>(null)
  const [splitScans, setSplitScans] = useState(false)
  const input = useRef<HTMLInputElement>(null)
  const target = useUploadTarget(caseId).data
  const upload = useUpload()

  function add(list: FileList | null) {
    if (!list) return
    setFiles((prev) => [
      ...prev,
      ...Array.from(list).filter((f) => !prev.some((p) => p.name === f.name && p.size === f.size)),
    ])
  }
  function onDrop(e: DragEvent) {
    e.preventDefault()
    setDragging(false)
    add(e.dataTransfer.files)
  }
  function close() {
    setFiles([])
    setResults(null)
    setParentId(null)
    setSplitScans(false)
    upload.reset()
    onClose()
  }

  return (
    <Modal
      open={open}
      onClose={close}
      title="Ingest documents"
      subtitle={
        target?.case_title
          ? `into ${caseId} · ${target.case_title}`
          : 'into triage · local processing'
      }
      icon="upload_file"
      width={520}
    >
      {results ? (
        <ul className="divide-y divide-line2">
          {results.map((r, i) => (
            <ResultRow key={`${r.filename}-${i}`} result={r} />
          ))}
        </ul>
      ) : (
        <div className="space-y-3">
          <button
            type="button"
            onClick={() => input.current?.click()}
            onDragOver={(e) => {
              e.preventDefault()
              setDragging(true)
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
            className={`flex w-full flex-col items-center gap-1 rounded-xl border border-dashed px-4 py-8 text-[12px] ${dragging ? 'border-accent bg-accent/8' : 'border-line3 hover:border-accent/60'}`}
          >
            <Icon name="cloud_upload" size={26} className="text-accent" />
            <span className="font-semibold">Drop files here or click to choose</span>
            <span className="font-mono text-[10px] text-muted">
              PDF · DOCX · TXT · MD · PPTX · XLSX · EML · max 50 MB
            </span>
          </button>
          <input
            ref={input}
            type="file"
            multiple
            accept={ACCEPT}
            className="hidden"
            aria-label="Choose files"
            onChange={(e) => {
              add(e.target.files)
              e.target.value = ''
            }}
          />
          {files.length > 0 && (
            <ul className="divide-y divide-line2 rounded-xl border border-line">
              {files.map((f) => (
                <li
                  key={`${f.name}-${f.size}`}
                  className="flex items-center gap-2 px-3 py-1.5 text-[12px]"
                >
                  <Icon
                    name={f.name.toLowerCase().endsWith('.eml') ? 'mail' : 'picture_as_pdf'}
                    size={16}
                    className="text-muted"
                  />
                  <span className="min-w-0 flex-1 truncate">{f.name}</span>
                  <span className="font-mono text-[10px] text-muted">
                    {(f.size / 1024).toFixed(0)} KB
                  </span>
                  <button
                    type="button"
                    aria-label={`Remove ${f.name}`}
                    onClick={() => setFiles((p) => p.filter((x) => x !== f))}
                    className="text-muted hover:text-danger"
                  >
                    <Icon name="close" size={14} />
                  </button>
                </li>
              ))}
            </ul>
          )}
          {target && target.parent_options.length > 0 && (
            <Field label="Attach as enclosure of" htmlFor="parent_id">
              <select
                id="parent_id"
                name="parent_id"
                className={inputClass}
                onChange={(e) => setParentId(e.target.value ? Number(e.target.value) : null)}
              >
                <option value="">— standalone —</option>
                {target.parent_options.map((p) => (
                  <option key={String(p.id)} value={String(p.id)}>
                    {String(p.title)}
                  </option>
                ))}
              </select>
            </Field>
          )}
          {caseId === null && files.some((f) => f.name.toLowerCase().endsWith('.pdf')) && (
            <label className="flex items-start gap-2 text-[12px]">
              <input
                type="checkbox"
                checked={splitScans}
                onChange={(e) => setSplitScans(e.target.checked)}
                className="mt-0.5"
              />
              <span>
                <span className="font-semibold">Scanned stack — split into documents</span>
                <span className="block text-[10.5px] text-muted">
                  Each PDF is checked for letter boundaries; you confirm the cuts before processing.
                  Other file types are ingested as usual.
                </span>
              </span>
            </label>
          )}
          <p className="flex items-center gap-1 text-[10.5px] text-muted">
            <Icon name="lock" size={12} /> Each file runs through the local pipeline: Extract ›
            Metadata › Enrich › Relationships › Claims › Entities › Embeddings.
          </p>
          {upload.error && (
            <p role="alert" className="text-[11px] text-danger">
              {upload.error.message}
            </p>
          )}
        </div>
      )}
      <div className="mt-3 flex justify-end gap-2">
        <Button variant="secondary" onClick={close}>
          {results ? 'Close' : 'Cancel'}
        </Button>
        {!results && (
          <Button
            disabled={files.length === 0 || upload.isPending}
            onClick={() =>
              upload.mutate(
                { files, caseId, parentId, splitScans: caseId === null && splitScans },
                { onSuccess: (r) => setResults(r.results) },
              )
            }
          >
            Ingest {files.length || ''}
          </Button>
        )}
      </div>
    </Modal>
  )
}

function ResultRow({ result }: { result: Schemas['UploadResult'] }) {
  return (
    <li className="flex items-center gap-2 py-1.5 text-[12px]">
      <Icon
        name={
          result.status === 'queued'
            ? 'schedule'
            : result.status === 'duplicate'
              ? 'history'
              : 'error'
        }
        size={16}
        className={result.status === 'error' ? 'text-danger' : 'text-muted'}
      />
      <span className="min-w-0 flex-1 truncate">{result.filename}</span>
      {result.status === 'queued' && result.slicing && result.batch_id ? (
        <a
          href={`/ingest/slice/${result.batch_id}`}
          className="font-mono text-[11px] text-tealink hover:underline"
        >
          Review cuts
        </a>
      ) : result.status === 'queued' && result.doc_id ? (
        <LiveStatus docId={result.doc_id} />
      ) : (
        <Badge tone={result.status === 'error' ? 'danger' : 'neutral'}>
          {result.message ?? result.status}
        </Badge>
      )}
    </li>
  )
}

function LiveStatus({ docId }: { docId: number }) {
  const status = useDocumentStatus(docId).data
  if (!status) return <Badge>queued</Badge>
  const tone =
    status.state === 'failed' ? 'danger' : status.state === 'completed' ? 'success' : 'warning'
  return (
    <Badge tone={tone} className="normal-case" mono>
      {status.label}
    </Badge>
  )
}
