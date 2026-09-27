import { useState, useRef, useEffect, useCallback } from 'react'
import { ImagePlus, X, Send, Save, RotateCcw } from 'lucide-react'
import { toast } from 'sonner'
import { PageHeader } from '@/components/PageHeader'
import { PostItem } from '@/components/PostItem'
import { EditModal } from '@/components/EditModal'
import { EmojiPicker } from '@/components/EmojiPicker'
import { TweetPreview } from '@/components/TweetPreview'
import { DateTimePicker } from '@/components/DateTimePicker'
import { useConfirm } from '@/components/ConfirmModal'
import { useComposer } from '@/contexts/ComposerContext'
import { useSettings } from '@/contexts/SettingsContext'
import { toastResult, outcomeOf } from '@/lib/utils'
import * as api from '@/lib/api'
import type { Post } from '@/lib/api'

export function Composer() {
  const { text, setText, scheduledAt, setScheduledAt, imageFile, setImageFile, imagePreview, setImagePreview, resetComposer } = useComposer()
  const { t } = useSettings()
  const confirm = useConfirm()
  const [loading, setLoading] = useState(false)
  const [drafts, setDrafts] = useState<Post[]>([])
  const [profile, setProfile] = useState<api.Profile | null>(null)
  const [editingPost, setEditingPost] = useState<Post | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const loadDrafts = useCallback(async () => {
    try {
      const posts = await api.fetchPosts('draft')
      setDrafts(posts)
    } catch { /* ignore */ }
  }, [])

  useEffect(() => {
    api.fetchProfile().then(setProfile).catch(() => {})
    loadDrafts()
  }, [loadDrafts])

  const charLimit = profile?.is_verified ? 25000 : 280

  const handleMedia = (file: File) => {
    if (file.type.startsWith('video/')) {
      toast.error(t('composer.videoNotSupported'))
      if (fileInputRef.current) fileInputRef.current.value = ''
      return
    }
    if (!file.type.startsWith('image/')) {
      toast.error(t('composer.imageOnly'))
      return
    }
    const maxSize = 5 * 1024 * 1024
    if (file.size > maxSize) {
      toast.error(t('composer.imageTooLarge'))
      return
    }
    setImageFile(file)
    const reader = new FileReader()
    reader.onload = e => setImagePreview(e.target?.result as string)
    reader.readAsDataURL(file)
  }

  const removeMedia = () => {
    if (imagePreview && !imagePreview.startsWith('data:')) URL.revokeObjectURL(imagePreview)
    setImageFile(null)
    setImagePreview(null)
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  const resetForm = () => {
    resetComposer()
    if (fileInputRef.current) fileInputRef.current.value = ''
    if (textareaRef.current) textareaRef.current.style.height = ''
  }

  const insertEmoji = (emoji: string) => {
    const ta = textareaRef.current
    if (!ta) {
      setText(text + emoji)
      return
    }
    const start = ta.selectionStart
    const end = ta.selectionEnd
    const before = text.slice(0, start)
    const after = text.slice(end)
    const newText = before + emoji + after
    setText(newText)
    requestAnimationFrame(() => {
      ta.focus()
      ta.selectionStart = ta.selectionEnd = start + emoji.length
    })
  }

  const submit = async (status: 'draft' | 'scheduled' | 'posting') => {
    if (!text.trim() && !imageFile) {
      toast.error(t('composer.needTextOrMedia'))
      return
    }
    if (text.length > charLimit) {
      toast.error(t('composer.tooLong'))
      return
    }
    if (status === 'scheduled' && !scheduledAt) {
      toast.error(t('composer.selectDate'))
      return
    }
    if (status === 'scheduled' && new Date(scheduledAt) < new Date(Date.now() + 5 * 60 * 1000)) {
      toast.error(t('composer.dateFuture'))
      return
    }

    setLoading(true)
    try {
      const formData = new FormData()
      formData.set('text', text)
      formData.set('status', status === 'posting' ? 'draft' : status)
      if (scheduledAt) formData.set('scheduled_at', scheduledAt)
      if (imageFile) formData.set('image', imageFile)

      const result = await api.createPost(formData)
      if (result.error) {
        toast.error(result.error)
        return
      }

      if (status === 'posting') {
        toast.info(t('composer.publishing'))
        await api.postNow(result.id)
        // Queued: free the editor now and report the outcome when it lands.
        resetForm()
        watchOutcome(result.id, 'posted', t('composer.published'))
      } else if (status === 'scheduled') {
        toast.info(t('composer.scheduling'))
        await api.scheduleNow(result.id)
        resetForm()
        watchOutcome(result.id, 'scheduled_on_x', t('composer.scheduledOnX'))
      } else {
        toast.success(t('composer.draftSaved'))
        resetForm()
      }
      loadDrafts()
    } catch {
      toast.error(t('common.connectionError'))
    } finally {
      setLoading(false)
    }
  }

  // Keep the latest submit in a ref so the shortcut listener is attached once
  // instead of being torn down and re-added on every keystroke.
  const submitRef = useRef(submit)
  submitRef.current = submit

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || loading) return
      if (e.key === 'Enter') { e.preventDefault(); submitRef.current('posting') }
      else if (e.key === 's') { e.preventDefault(); submitRef.current('draft') }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [loading])

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    const file = e.dataTransfer.files[0]
    if (file?.type.startsWith('image/') || file?.type.startsWith('video/')) {
      handleMedia(file)
      const dt = new DataTransfer()
      dt.items.add(file)
      if (fileInputRef.current) fileInputRef.current.files = dt.files
    }
  }

  /** Follow a queued post and toast the real outcome when the worker is done. */
  const watchOutcome = useCallback(
    (id: number, expected: Post['status'], successMessage: string) => {
      api.waitForPost(id)
        .then(post => toastResult(outcomeOf(post, expected), successMessage, t))
        .catch(() => toast.error(t('common.serverError')))
        .finally(() => loadDrafts())
    },
    [t, loadDrafts],
  )

  const handlePostAction = async (action: string, id: number) => {
    try {
      if (action === 'post-now') {
        if (!await confirm({ message: t('composer.confirmPublish') })) return
        toast.info(t('composer.publishing'))
        await api.postNow(id)
        watchOutcome(id, 'posted', t('post.published'))
      } else if (action === 'duplicate') {
        await api.duplicatePost(id)
        toast.success(t('composer.duplicated'))
      } else if (action === 'delete') {
        if (!await confirm({ message: t('composer.confirmDelete'), danger: true })) return
        await api.deletePost(id)
        toast.success(t('composer.deleted'))
      }
      loadDrafts()
    } catch { toast.error(t('common.serverError')) }
  }

  const handleSaveEdit = async (id: number, data: { text: string; scheduled_at: string; status: Post['status'] }) => {
    try {
      await api.updatePost(id, data)
      toast.success(t('composer.updated'))
      setEditingPost(null)
      loadDrafts()
    } catch { toast.error(t('common.serverError')) }
  }

  const charWarning = text.length > charLimit ? 'text-error font-semibold' : text.length > charLimit - 30 ? 'text-warning' : 'text-text-muted'

  return (
    <div>
      <PageHeader title={t('composer.title')} description={t('composer.desc')} />

      <div className="flex divide-x divide-border">
        {/* Editor column */}
        <div className="flex-1 min-w-0">
          <div
            className="px-6 py-5"
            onDragOver={e => e.preventDefault()}
            onDrop={handleDrop}
          >
            <div className="flex gap-3">
              <img
                src={api.profilePictureUrl()}
                alt=""
                className="w-9 h-9 rounded-full bg-bg-secondary object-cover shrink-0"
                onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
              />
              <div className="flex-1 min-w-0">
                <textarea
                  ref={textareaRef}
                  value={text}
                  onChange={e => {
                    setText(e.target.value)
                    e.target.style.height = 'auto'
                    e.target.style.height = Math.max(120, e.target.scrollHeight) + 'px'
                  }}
                  placeholder={t('composer.placeholder')}
                  maxLength={charLimit}
                  rows={4}
                  className="w-full resize-none border-0 outline-none text-[15px] leading-relaxed text-text bg-bg-secondary/40 rounded-lg p-3 placeholder:text-text-muted/60 min-h-[120px]"
                />

                {imagePreview && (
                  <div className="relative mt-3 inline-block">
                    <img src={imagePreview} alt="" className="max-h-48 rounded-lg border border-border object-cover" />
                    <button
                      onClick={removeMedia}
                      className="absolute top-2 right-2 w-6 h-6 bg-black/60 text-white rounded-full flex items-center justify-center hover:bg-black/80 transition-colors"
                    >
                      <X size={14} />
                    </button>
                  </div>
                )}

                {/* Toolbar */}
                <div className="flex items-center justify-between mt-4 pt-3 border-t border-border">
                  <div className="flex items-center gap-1">
                    <input
                      ref={fileInputRef}
                      type="file"
                      accept="image/*,video/*"
                      className="hidden"
                      onChange={e => e.target.files?.[0] && handleMedia(e.target.files[0])}
                    />
                    <button
                      onClick={() => fileInputRef.current?.click()}
                      className="w-8 h-8 flex items-center justify-center rounded-md text-accent hover:bg-accent-light transition-colors"
                      title={t('composer.addMedia')}
                    >
                      <ImagePlus size={18} />
                    </button>
                    <EmojiPicker onSelect={insertEmoji} />
                    <span className={`text-xs font-mono ml-2 ${charWarning}`}>{text.length.toLocaleString()}/{charLimit.toLocaleString()}</span>
                  </div>

                  <div className="flex items-center gap-2">
                    <button
                      onClick={resetForm}
                      disabled={loading}
                      className="px-3 py-1.5 text-xs text-text-muted hover:text-text rounded-md hover:bg-bg-hover transition-colors disabled:opacity-40"
                    >
                      <RotateCcw size={13} className="inline mr-1" />
                      {t('common.reset')}
                    </button>
                    <button
                      onClick={() => submit('draft')}
                      disabled={loading}
                      className="px-3 py-1.5 text-xs font-medium text-text-secondary border border-border rounded-md hover:bg-bg-hover transition-colors disabled:opacity-40"
                    >
                      <Save size={13} className="inline mr-1" />
                      {t('composer.draft')}
                    </button>
                    <button
                      onClick={() => submit('posting')}
                      disabled={loading}
                      className="px-4 py-1.5 text-xs font-semibold text-white bg-accent rounded-md hover:bg-accent-hover transition-colors disabled:opacity-40 shadow-sm"
                    >
                      <Send size={13} className="inline mr-1" />
                      {t('common.publish')}
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/* Schedule bar */}
          <div className="px-6 py-3 border-t border-border bg-bg-secondary flex items-center gap-3">
            <label className="text-xs text-text-secondary font-medium shrink-0">{t('composer.schedule')}</label>
            <DateTimePicker value={scheduledAt} onChange={setScheduledAt} />
            <button
              onClick={() => submit('scheduled')}
              disabled={loading || !scheduledAt}
              className="px-3 py-1.5 text-xs font-medium text-white bg-accent rounded-md hover:bg-accent-hover transition-colors disabled:opacity-40"
            >
              {t('composer.schedule')}
            </button>
          </div>

          {/* Drafts */}
          {drafts.length > 0 && (
            <div className="border-t border-border">
              <div className="px-6 py-3 bg-bg-secondary border-b border-border">
                <h3 className="text-xs font-semibold text-text-secondary uppercase tracking-wider">{t('composer.drafts')} ({drafts.length})</h3>
              </div>
              {drafts.map(p => (
                <PostItem
                  key={p.id}
                  post={p}
                  actions={['edit', 'post-now', 'duplicate', 'delete']}
                  onEdit={setEditingPost}
                  onPostNow={id => handlePostAction('post-now', id)}
                  onDuplicate={id => handlePostAction('duplicate', id)}
                  onDelete={id => handlePostAction('delete', id)}
                />
              ))}
            </div>
          )}
        </div>

        {/* Preview column */}
        <div className="w-[380px] shrink-0 p-6 bg-bg-secondary hidden lg:block">
          <h3 className="text-xs font-semibold text-text-secondary uppercase tracking-wider mb-4">{t('composer.preview')}</h3>
          <TweetPreview
            text={text}
            imageUrl={imagePreview}
            scheduledAt={scheduledAt || null}
            profile={profile}
          />
        </div>
      </div>

      {editingPost && (
        <EditModal
          post={editingPost}
          charLimit={charLimit}
          onSave={handleSaveEdit}
          onClose={() => setEditingPost(null)}
        />
      )}
    </div>
  )
}
