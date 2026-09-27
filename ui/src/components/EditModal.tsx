import { useState } from 'react'
import { MediaThumb } from '@/components/MediaThumb'
import { X, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import type { Post } from '@/lib/api'
import * as api from '@/lib/api'
import { useSettings } from '@/contexts/SettingsContext'

interface EditModalProps {
  post: Post
  charLimit?: number
  onSave: (id: number, data: { text: string; scheduled_at: string; status: Post['status'] }) => void
  onClose: () => void
}

export function EditModal({ post, charLimit = 280, onSave, onClose }: EditModalProps) {
  const { t } = useSettings()
  const [text, setText] = useState(post.text || '')
  const [scheduledAt, setScheduledAt] = useState(post.scheduled_at?.substring(0, 16) || '')
  const [status, setStatus] = useState<Post['status']>(post.status === 'error' ? 'draft' : post.status)
  const [hasMedia, setHasMedia] = useState(!!post.image_path)
  const thumbFile = post.image_path?.split(/[/\\]/).pop()

  const handleRemoveMedia = async () => {
    if (!confirm(t('post.confirmRemoveMedia'))) return
    try {
      await api.removeMedia(post.id)
      setHasMedia(false)
      toast.success(t('post.mediaRemoved'))
    } catch {
      toast.error(t('common.serverError'))
    }
  }

  const handleSave = () => {
    if (text.length > charLimit) return
    if (status === 'scheduled' && !scheduledAt) return
    onSave(post.id, { text, scheduled_at: scheduledAt, status })
  }

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center" onClick={onClose}>
      <div
        className="bg-bg rounded-xl shadow-lg w-full max-w-md mx-4 overflow-hidden"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-5 py-3.5 border-b border-border">
          <h3 className="text-sm font-semibold">{t('edit.title')} #{post.id}</h3>
          <button onClick={onClose} className="text-text-muted hover:text-text transition-colors">
            <X size={18} />
          </button>
        </div>

        <div className="px-5 py-4 space-y-4">
          <div>
            <label className="block text-xs font-medium text-text-secondary mb-1.5">{t('edit.text')}</label>
            <textarea
              value={text}
              onChange={e => setText(e.target.value)}
              rows={4}
              maxLength={charLimit}
              className="w-full px-3 py-2 text-sm border border-border rounded-lg resize-none focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent/20 bg-bg"
            />
            <div className={`text-xs mt-1 text-right ${text.length > charLimit ? 'text-error font-medium' : 'text-text-muted'}`}>
              {text.length.toLocaleString()}/{charLimit.toLocaleString()}
            </div>
          </div>

          {hasMedia && thumbFile && (
            <div>
              <label className="block text-xs font-medium text-text-secondary mb-1.5">{t('edit.media')}</label>
              <div className="flex items-center gap-3">
                <div className="relative inline-block">
                  <MediaThumb
                    file={thumbFile}
                    className="w-20 h-14 object-cover rounded-lg border border-border"
                  />
                </div>
                <button
                  onClick={handleRemoveMedia}
                  className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium text-error border border-error/30 rounded-lg hover:bg-error-light transition-colors"
                >
                  <Trash2 size={12} />
                  {t('post.removeMedia')}
                </button>
              </div>
            </div>
          )}

          <div>
            <label className="block text-xs font-medium text-text-secondary mb-1.5">{t('edit.scheduledDate')}</label>
            <input
              type="datetime-local"
              value={scheduledAt}
              onChange={e => setScheduledAt(e.target.value)}
              className="w-full px-3 py-2 text-sm border border-border rounded-lg focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent/20 bg-bg"
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-text-secondary mb-1.5">{t('edit.status')}</label>
            <select
              value={status}
              onChange={e => setStatus(e.target.value as Post['status'])}
              className="w-full px-3 py-2 text-sm border border-border rounded-lg focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent/20 bg-bg"
            >
              <option value="draft">{t('status.draft')}</option>
              <option value="scheduled">{t('status.scheduled')}</option>
            </select>
          </div>
        </div>

        <div className="flex justify-end gap-2 px-5 py-3.5 border-t border-border bg-bg-secondary">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm font-medium text-text-secondary hover:text-text hover:bg-bg rounded-lg transition-colors"
          >
            {t('common.cancel')}
          </button>
          <button
            onClick={handleSave}
            className="px-4 py-2 text-sm font-medium text-white bg-accent rounded-lg hover:bg-accent-hover transition-colors"
          >
            {t('common.save')}
          </button>
        </div>
      </div>
    </div>
  )
}
