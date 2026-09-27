import { useState, useEffect, useCallback } from 'react'
import { CalendarClock, Pencil, Play, Copy, Trash2, X } from 'lucide-react'
import { toast } from 'sonner'
import { PageHeader } from '@/components/PageHeader'
import { EditModal } from '@/components/EditModal'
import { useConfirm } from '@/components/ConfirmModal'
import { useSettings } from '@/contexts/SettingsContext'
import * as api from '@/lib/api'
import type { Post } from '@/lib/api'
import { timeFromNow, cn, toastResult, outcomeOf } from '@/lib/utils'
import type { TranslationKey } from '@/lib/i18n'

const statusKeys: Record<Post['status'], TranslationKey> = {
  draft: 'status.draft',
  scheduled: 'status.scheduled',
  scheduling: 'status.scheduling',
  scheduled_on_x: 'status.scheduled_on_x',
  posting: 'status.posting',
  posted: 'status.posted',
  error: 'status.error',
}

// Display order of the pipeline on this page.
const PIPELINE_STATUSES: Post['status'][] = ['posting', 'scheduling', 'scheduled', 'scheduled_on_x']

export function Schedule() {
  const { t, locale } = useSettings()
  const confirm = useConfirm()
  const [posts, setPosts] = useState<Post[]>([])
  const [loading, setLoading] = useState(true)
  const [editingPost, setEditingPost] = useState<Post | null>(null)
  const [profile, setProfile] = useState<api.Profile | null>(null)
  const charLimit = profile?.is_verified ? 25000 : 280

  const load = useCallback(async () => {
    try {
      // One request for all four statuses, then ordered the way the page shows
      // them: in-flight first, then waiting, then already handed to X.
      const all = await api.fetchPosts(PIPELINE_STATUSES)
      const rank = (p: Post) => PIPELINE_STATUSES.indexOf(p.status)
      setPosts([...all].sort((a, b) => rank(a) - rank(b)))
    } catch {
      toast.error(t('common.loadingError'))
    } finally {
      setLoading(false)
    }
  }, [t])

  // Polling plus rapide quand des posts sont en cours de traitement
  const hasProcessingPosts = posts.some(p => p.status === 'scheduling' || p.status === 'posting')
  const pollInterval = hasProcessingPosts ? 3000 : 15000

  useEffect(() => {
    api.fetchProfile().then(setProfile).catch(() => {})
    load()
  }, [load])

  useEffect(() => {
    const interval = setInterval(load, pollInterval)
    return () => clearInterval(interval)
  }, [load, pollInterval])

  const handleAction = async (action: string, id: number) => {
    try {
      if (action === 'post-now') {
        if (!await confirm({ message: t('composer.confirmPublish') })) return
        toast.info(t('composer.publishing'))
        await api.postNow(id)
        // The page already polls, so the row updates on its own meanwhile.
        api.waitForPost(id)
          .then(post => toastResult(outcomeOf(post, 'posted'), t('post.published'), t))
          .catch(() => toast.error(t('common.serverError')))
          .finally(() => load())
      } else if (action === 'duplicate') {
        await api.duplicatePost(id)
        toast.success(t('composer.duplicated'))
      } else if (action === 'remove-media') {
        if (!await confirm({ message: t('post.confirmRemoveMedia'), danger: true })) return
        await api.removeMedia(id)
        toast.success(t('post.mediaRemoved'))
      } else if (action === 'delete-from-x') {
        if (!await confirm({ message: t('schedule.confirmDeleteFromX'), danger: true })) return
        toast.info(t('schedule.deletingFromX'))
        const r = await api.deleteScheduledFromX(id)
        toastResult(r, t('schedule.deletedFromX'), t)
      } else if (action === 'delete') {
        if (!await confirm({ message: t('composer.confirmDelete'), danger: true })) return
        await api.deletePost(id)
        toast.success(t('composer.deleted'))
      }
      load()
    } catch {
      toast.error(t('common.serverError'))
    }
  }

  const handleSaveEdit = async (id: number, data: { text: string; scheduled_at: string; status: Post['status'] }) => {
    try {
      await api.updatePost(id, data)
      toast.success(t('composer.updated'))
      setEditingPost(null)
      load()
    } catch {
      toast.error(t('common.serverError'))
    }
  }

  // Group posts by date
  const grouped: Record<string, Post[]> = {}
  posts.forEach(p => {
    const dateKey = p.scheduled_at ? p.scheduled_at.slice(0, 10) : t('schedule.noDate')
    if (!grouped[dateKey]) grouped[dateKey] = []
    grouped[dateKey].push(p)
  })
  const sortedDates = Object.keys(grouped).sort()

  const localeDateOptions: Intl.DateTimeFormatOptions = { weekday: 'long', day: 'numeric', month: 'long' }
  const localeTimeOptions: Intl.DateTimeFormatOptions = { hour: '2-digit', minute: '2-digit' }
  const localeCode = locale === 'fr' ? 'fr-FR' : 'en-US'

  return (
    <div>
      <PageHeader
        title={t('schedule.title')}
        description={`${posts.length} ${t('schedule.count')}`}
      />

      {loading ? (
        <div className="px-6 py-16 text-center text-sm text-text-muted">{t('common.loading')}</div>
      ) : posts.length === 0 ? (
        <div className="px-6 py-16 text-center">
          <CalendarClock size={40} className="mx-auto text-text-muted/40 mb-3" />
          <p className="text-sm text-text-secondary font-medium">{t('schedule.empty')}</p>
          <p className="text-xs text-text-muted mt-1">{t('schedule.emptyHint')}</p>
        </div>
      ) : (
        <div className="px-6 py-4 space-y-6">
          {sortedDates.map(dateKey => {
            const datePosts = grouped[dateKey]
            const dateLabel = dateKey === t('schedule.noDate') ? dateKey : new Date(dateKey + 'T00:00:00').toLocaleDateString(localeCode, localeDateOptions)
            return (
              <div key={dateKey}>
                <h3 className="text-xs font-semibold text-text-secondary uppercase tracking-wider mb-3 capitalize">{dateLabel}</h3>
                <div className="space-y-2">
                  {datePosts.map(post => {
                    const isPosting = post.status === 'posting'
                    const thumbFile = post.image_path?.split(/[/\\]/).pop()
                    return (
                      <div
                        key={post.id}
                        className={cn(
                          'flex items-start gap-4 p-4 rounded-xl border border-border bg-bg-secondary/50 hover:border-border-light hover:shadow-sm transition-all',
                          isPosting && 'opacity-60'
                        )}
                      >
                        {/* Time badge */}
                        <div className="shrink-0 w-16 text-center pt-0.5">
                          <div className="text-sm font-semibold text-text">
                            {post.scheduled_at ? new Date(post.scheduled_at).toLocaleTimeString(localeCode, localeTimeOptions) : '--:--'}
                          </div>
                          <div className="text-[10px] text-accent font-medium mt-0.5">
                            {post.scheduled_at ? timeFromNow(post.scheduled_at, locale) : ''}
                          </div>
                        </div>

                        {/* Content */}
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2 mb-1">
                            <span className={cn(
                              'text-[11px] font-medium px-2 py-0.5 rounded-full',
                              (post.status === 'posting' || post.status === 'scheduling') ? 'bg-warning-light text-warning'
                                : post.status === 'scheduled_on_x' ? 'bg-success-light text-success'
                                : 'bg-accent-light text-accent'
                            )}>
                              {t(statusKeys[post.status])}
                            </span>
                          </div>
                          <p className="text-sm font-medium text-text leading-relaxed line-clamp-2 whitespace-pre-wrap break-words">
                            {post.text || <em className="text-text-muted">{t('post.mediaOnly')}</em>}
                          </p>
                          {thumbFile && (
                            <div className="relative mt-2 inline-block">
                              <img
                                src={api.uploadUrl(thumbFile)}
                                alt=""
                                className="w-16 h-12 object-cover rounded-md border border-border"
                                loading="lazy"
                              />
                              <button
                                disabled={isPosting}
                                onClick={() => handleAction('remove-media', post.id)}
                                className="absolute -top-1.5 -right-1.5 w-5 h-5 bg-black/70 text-white rounded-full flex items-center justify-center hover:bg-error transition-colors disabled:opacity-30"
                                title={t('post.removeMedia')}
                              >
                                <X size={10} />
                              </button>
                            </div>
                          )}
                        </div>

                        {/* Actions */}
                        <div className="flex items-center gap-1 shrink-0">
                          <button
                            disabled={isPosting}
                            onClick={() => setEditingPost(post)}
                            className="p-1.5 rounded-md text-text-muted hover:text-text hover:bg-bg-hover transition-colors disabled:opacity-30"
                            title={t('common.edit')}
                          >
                            <Pencil size={14} />
                          </button>
                          <button
                            disabled={isPosting}
                            onClick={() => handleAction('post-now', post.id)}
                            className="p-1.5 rounded-md text-text-muted hover:text-accent hover:bg-accent-light transition-colors disabled:opacity-30"
                            title={t('post.publishNow')}
                          >
                            <Play size={14} />
                          </button>
                          <button
                            disabled={isPosting}
                            onClick={() => handleAction('duplicate', post.id)}
                            className="p-1.5 rounded-md text-text-muted hover:text-text hover:bg-bg-hover transition-colors disabled:opacity-30"
                            title={t('common.duplicate')}
                          >
                            <Copy size={14} />
                          </button>
                          <button
                            onClick={() => handleAction(post.status === 'scheduled_on_x' && post.text ? 'delete-from-x' : 'delete', post.id)}
                            className="p-1.5 rounded-md text-text-muted hover:text-error hover:bg-error-light transition-colors"
                            title={post.status === 'scheduled_on_x' && post.text ? t('schedule.deleteFromX') : t('common.delete')}
                          >
                            <Trash2 size={14} />
                          </button>
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>
            )
          })}
        </div>
      )}

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
