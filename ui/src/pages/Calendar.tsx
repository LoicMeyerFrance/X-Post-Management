import { useState, useEffect, useCallback } from 'react'
import { ChevronLeft, ChevronRight, X, Eye, EyeOff, ExternalLink } from 'lucide-react'
import { toast } from 'sonner'
import { PageHeader } from '@/components/PageHeader'
import { PostItem } from '@/components/PostItem'
import { EditModal } from '@/components/EditModal'
import { TweetPreview } from '@/components/TweetPreview'
import { useConfirm } from '@/components/ConfirmModal'
import { useSettings } from '@/contexts/SettingsContext'
import { MONTHS_LONG, DAYS_LONG } from '@/lib/i18n'
import * as api from '@/lib/api'
import type { Post } from '@/lib/api'
import { toastResult, outcomeOf , isVideoFile, localDateKey } from '@/lib/utils'
import { cn } from '@/lib/utils'

const statusDotColors: Record<string, string> = {
  posted: 'bg-success',
  scheduled: 'bg-accent',
  scheduled_on_x: 'bg-accent',
  scheduling: 'bg-warning',
  draft: 'bg-border',
  error: 'bg-error',
  posting: 'bg-warning',
}

export function Calendar() {
  const { t, locale } = useSettings()
  const confirm = useConfirm()
  const [year, setYear] = useState(() => new Date().getFullYear())
  const [month, setMonth] = useState(() => new Date().getMonth())
  const [posts, setPosts] = useState<Post[]>([])
  const [selectedDate, setSelectedDate] = useState<string | null>(null)
  const [editingPost, setEditingPost] = useState<Post | null>(null)
  const [previewId, setPreviewId] = useState<number | null>(null)
  const [profile, setProfile] = useState<api.Profile | null>(null)
  // Tweets read back from X that this app never sent. Off by default: a long
  // history would bury the posts the user actually manages here.
  const [xTweets, setXTweets] = useState<api.XTweet[]>([])
  const [showFromX, setShowFromX] = useState(false)
  const charLimit = profile?.is_verified ? 25000 : 280

  const localeCode = locale === 'fr' ? 'fr-FR' : 'en-US'

  useEffect(() => {
    api.fetchProfile().then(setProfile).catch(() => {})
  }, [])

  useEffect(() => {
    if (previewId === null) return
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setPreviewId(null)
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [previewId])

  const load = useCallback(async () => {
    try {
      const data = await api.fetchPosts()
      setPosts(data)
    } catch { /* ignore */ }
  }, [])

  const loadXTweets = useCallback(async () => {
    try {
      const data = await api.fetchXHistory()
      setXTweets(data.tweets)
    } catch { /* the calendar still works without the mirror */ }
  }, [])

  // The preference lives on the server: localStorage does not survive pywebview.
  // Restoring it and fetching the mirror is one chain, so the state only changes
  // once the data is in hand.
  useEffect(() => {
    api.fetchPreferences()
      .then(async prefs => {
        if (prefs.calendarShowFromX !== 'true') return
        await loadXTweets()
        setShowFromX(true)
      })
      .catch(() => {})
  }, [loadXTweets])

  const toggleFromX = async () => {
    const next = !showFromX
    if (next && xTweets.length === 0) await loadXTweets()
    setShowFromX(next)
    api.savePreferences({ calendarShowFromX: next ? 'true' : 'false' }).catch(() => {})
  }

  // load() is async: setPosts runs after the fetch resolves, never during the
  // effect body.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { load() }, [load])

  const prev = () => {
    if (month === 0) { setMonth(11); setYear(y => y - 1) }
    else setMonth(m => m - 1)
    setSelectedDate(null)
  }

  const next = () => {
    if (month === 11) { setMonth(0); setYear(y => y + 1) }
    else setMonth(m => m + 1)
    setSelectedDate(null)
  }

  // Group posts by date
  const postsByDate: Record<string, Post[]> = {}
  posts.forEach(p => {
    let d: string | null = null
    if (p.status === 'posted' && p.posted_at) d = localDateKey(p.posted_at)
    else if ((p.status === 'scheduled' || p.status === 'scheduled_on_x' || p.status === 'scheduling') && p.scheduled_at) d = localDateKey(p.scheduled_at)
    else if (p.status === 'error' && p.scheduled_at) d = localDateKey(p.scheduled_at)
    else if (p.status === 'draft' && p.created_at) d = localDateKey(p.created_at)
    if (d) {
      if (!postsByDate[d]) postsByDate[d] = []
      postsByDate[d].push(p)
    }
  })

  // Only tweets this app did not send. One sent from here already has a row in
  // `posts`, and app_post_id is what pairs them - showing both would count the
  // same tweet twice on the same day.
  const foreignByDate: Record<string, api.XTweet[]> = {}
  if (showFromX) {
    xTweets.forEach(tweet => {
      if (tweet.app_post_id) return
      const d = localDateKey(tweet.posted_at)
      if (!d) return
      if (!foreignByDate[d]) foreignByDate[d] = []
      foreignByDate[d].push(tweet)
    })
  }
  const foreignCount = Object.values(foreignByDate).reduce((n, list) => n + list.length, 0)

  // Derived, not stored: snapshots here went stale the moment a post was deleted
  // or published, leaving a row that only disappeared on page change. The
  // preview follows the same rule, so it closes on its own when its post goes.
  const selectedPosts = selectedDate ? postsByDate[selectedDate] || [] : []
  const selectedForeign = selectedDate ? foreignByDate[selectedDate] || [] : []
  const previewPost = previewId !== null ? posts.find(p => p.id === previewId) ?? null : null

  const firstDay = new Date(year, month, 1).getDay()
  const startOffset = firstDay === 0 ? 6 : firstDay - 1
  const daysInMonth = new Date(year, month + 1, 0).getDate()
  const today = new Date()
  const todayStr = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`

  const cells: (number | null)[] = []
  for (let i = 0; i < startOffset; i++) cells.push(null)
  for (let d = 1; d <= daysInMonth; d++) cells.push(d)
  const remaining = cells.length % 7 === 0 ? 0 : 7 - (cells.length % 7)
  for (let i = 0; i < remaining; i++) cells.push(null)

  const selectDay = (day: number) => {
    setSelectedDate(
      `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`,
    )
  }

  const watchPost = (id: number) => {
    api.waitForPost(id)
      .then(post => toastResult(outcomeOf(post, 'posted'), t('post.published'), t))
      .catch(() => toast.error(t('common.serverError')))
      .finally(() => load())
  }

  const handleAction = async (action: string, id: number) => {
    try {
      if (action === 'post-now') {
        if (!await confirm({ message: t('composer.confirmPublish') })) return
        toast.info(t('composer.publishing'))
        await api.postNow(id)
        watchPost(id)
      } else if (action === 'retry') {
        toast.info(t('history.retrying'))
        await api.retryPost(id)
        watchPost(id)
      } else if (action === 'duplicate') {
        await api.duplicatePost(id)
        toast.success(t('composer.duplicated'))
      } else if (action === 'delete') {
        if (!await confirm({ message: t('calendar.confirmDelete'), danger: true })) return
        await api.deletePost(id)
        toast.success(t('composer.deleted'))
      } else if (action === 'delete-from-x') {
        const post = posts.find(p => p.id === id)
        if (post?.status === 'scheduled_on_x') {
          if (!await confirm({ message: t('schedule.confirmDeleteFromX'), danger: true })) return
          toast.info(t('schedule.deletingFromX'))
          const r = await api.deleteScheduledFromX(id)
          toastResult(r, t('schedule.deletedFromX'), t)
        } else {
          if (!await confirm({ message: t('history.confirmDeleteFromX'), danger: true })) return
          toast.info(t('history.deletingFromX'))
          const r = await api.deleteFromX(id)
          if (r.success) {
            toast.success(r.already_deleted ? t('history.alreadyDeleted') : t('history.deletedFromX'))
          } else {
            toast.error(`${t('common.errorPrefix')} : ${r.error || t('common.unknownError')}`)
          }
        }
      }
      load()
    } catch { toast.error(t('common.serverError')) }
  }

  const handleSaveEdit = async (id: number, data: { text: string; scheduled_at: string; status: Post['status'] }) => {
    try {
      await api.updatePost(id, data)
      toast.success(t('composer.updated'))
      setEditingPost(null)
      load()
    } catch { toast.error(t('common.serverError')) }
  }

  const actionsForStatus = (status: string) => {
    const map: Record<string, ('edit' | 'post-now' | 'duplicate' | 'delete' | 'retry' | 'view-on-x' | 'delete-from-x')[]> = {
      draft: ['edit', 'post-now', 'duplicate', 'delete'],
      scheduled: ['edit', 'post-now', 'duplicate', 'delete'],
      scheduled_on_x: ['edit', 'post-now', 'duplicate', 'delete-from-x'],
      scheduling: [],
      posted: ['view-on-x', 'delete-from-x', 'duplicate'],
      error: ['retry', 'duplicate', 'delete'],
      posting: [],
    }
    return map[status] || ['delete']
  }

  return (
    <div>
      <PageHeader title={t('calendar.title')} description={t('calendar.desc')} />

      {/* Toolbar */}
      <div className="flex items-center justify-between px-6 py-3 border-b border-border">
        <button onClick={prev} className="p-2 rounded-md hover:bg-bg-hover transition-colors">
          <ChevronLeft size={20} className="text-text-secondary" />
        </button>
        <h3 className="text-sm font-semibold text-text capitalize">{MONTHS_LONG[locale][month]} {year}</h3>
        <div className="flex items-center gap-2">
          <button
            onClick={toggleFromX}
            title={t('calendar.fromXHint')}
            className={cn(
              'inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-[11px] font-medium transition-colors',
              showFromX
                ? 'border-text-muted/40 bg-bg-secondary text-text-secondary'
                : 'border-border text-text-muted hover:bg-bg-hover'
            )}
          >
            {showFromX ? <Eye size={12} /> : <EyeOff size={12} />}
            {t('calendar.fromX')}
            {showFromX && foreignCount > 0 && (
              <span className="font-mono">{foreignCount}</span>
            )}
          </button>
          <button onClick={next} className="p-2 rounded-md hover:bg-bg-hover transition-colors">
            <ChevronRight size={20} className="text-text-secondary" />
          </button>
        </div>
      </div>

      {/* Legend */}
      <div className="flex items-center gap-4 px-6 py-2.5 border-b border-border text-[13px] text-text-muted">
        <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-success" /> {t('calendar.published')}</span>
        <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-accent" /> {t('calendar.scheduled')}</span>
        <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-border" /> {t('calendar.draft')}</span>
        <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-error" /> {t('calendar.error')}</span>
        {showFromX && (
          <span className="flex items-center gap-1.5">
            <span className="w-2 h-2 rounded-full border border-text-muted" /> {t('calendar.fromXLegend')}
          </span>
        )}
      </div>

      {/* Calendar grid */}
      <div className="grid grid-cols-7">
        {DAYS_LONG[locale].map(d => (
          <div key={d} className="py-2 text-center text-xs font-semibold text-text-muted border-b border-border">
            {d}
          </div>
        ))}
        {cells.map((day, i) => {
          if (day === null) {
            return <div key={`empty-${i}`} className="min-h-[90px] border-b border-r border-border bg-bg-secondary" />
          }
          const dateStr = `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`
          const dayPosts = postsByDate[dateStr] || []
          const dayForeign = foreignByDate[dateStr] || []
          const hasSomething = dayPosts.length > 0 || dayForeign.length > 0
          const isToday = dateStr === todayStr
          const isSelected = dateStr === selectedDate
          return (
            <div
              key={dateStr}
              onClick={() => hasSomething && selectDay(day)}
              className={cn(
                'min-h-[90px] p-2 border-b border-r border-border transition-colors relative',
                hasSomething && 'cursor-pointer hover:bg-accent-light/50 bg-bg-secondary/30',
                isSelected && 'bg-accent-light/70 ring-1 ring-inset ring-accent/30',
                i % 7 === 6 && 'border-r-0'
              )}
            >
              <span className={cn(
                'text-xs font-semibold',
                isToday && 'bg-accent text-white w-6 h-6 rounded-full inline-flex items-center justify-center shadow-sm',
                !isToday && 'text-text'
              )}>
                {day}
              </span>
              {hasSomething && (
                <div className="flex flex-wrap gap-1 mt-1.5">
                  {dayPosts.slice(0, 4).map(p => (
                    <span key={p.id} className={cn('w-2 h-2 rounded-full', statusDotColors[p.status])} />
                  ))}
                  {/* Hollow: read back from X, not sent from here. */}
                  {dayForeign.slice(0, 4).map(tw => (
                    <span key={tw.tweet_id}
                          className="w-2 h-2 rounded-full border border-text-muted" />
                  ))}
                  {dayPosts.length + dayForeign.length > 4 && (
                    <span className="text-[10px] text-text-muted font-medium">
                      +{dayPosts.length + dayForeign.length - 4}
                    </span>
                  )}
                </div>
              )}
              {dayPosts.length === 0 && dayForeign.length > 0 && (
                <div className="mt-1 space-y-0.5">
                  {dayForeign.slice(0, 2).map(tw => (
                    <p key={tw.tweet_id} className="text-[10px] text-text-muted/80 truncate leading-tight italic">
                      {tw.text?.substring(0, 30) || t('calendar.media')}
                    </p>
                  ))}
                </div>
              )}
              {dayPosts.length > 0 && (
                <div className="mt-1 space-y-0.5">
                  {dayPosts.slice(0, 2).map(p => (
                    <p key={p.id} className="text-[10px] text-text-muted truncate leading-tight">
                      {p.text?.substring(0, 30) || t('calendar.media')}
                    </p>
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </div>

      {/* Detail section */}
      {selectedDate && (selectedPosts.length > 0 || selectedForeign.length > 0) && (
        <div className="border-t border-border">
          <div className="px-6 py-3 bg-bg-secondary border-b border-border">
            <h3 className="text-xs font-semibold text-text-secondary uppercase tracking-wider capitalize">
              {new Date(selectedDate + 'T00:00:00').toLocaleDateString(localeCode, {
                weekday: 'long', day: 'numeric', month: 'long', year: 'numeric'
              })}
            </h3>
          </div>
          {selectedPosts.map(p => (
            <PostItem
              key={p.id}
              post={p}
              actions={actionsForStatus(p.status)}
              onClick={(p: Post) => setPreviewId(p.id)}
              onEdit={setEditingPost}
              onPostNow={id => handleAction('post-now', id)}
              onRetry={id => handleAction('retry', id)}
              onDuplicate={id => handleAction('duplicate', id)}
              onDelete={id => handleAction('delete', id)}
              onDeleteFromX={id => handleAction('delete-from-x', id)}
            />
          ))}
          {/* Read-only: these were not sent from here, so there is nothing to
              edit, reschedule or retry. The link goes to the tweet itself. */}
          {selectedForeign.map(tw => (
            <div key={tw.tweet_id}
                 className="flex items-start gap-3 border-b border-border px-6 py-3 last:border-b-0">
              <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full border border-text-muted" />
              <div className="min-w-0 flex-1">
                <div className="mb-0.5 flex items-center gap-2 text-[11px] text-text-muted">
                  <span>{tw.posted_at ? new Date(tw.posted_at).toLocaleTimeString(localeCode, { hour: '2-digit', minute: '2-digit' }) : ''}</span>
                  <span className="rounded-full bg-bg-secondary px-2 py-0.5">{t('history.elsewhere')}</span>
                  {tw.views && <span>{tw.views} {t('history.views')}</span>}
                </div>
                <p className="truncate text-[13px] text-text-secondary">
                  {tw.text || t('calendar.media')}
                </p>
              </div>
              <a href={tw.url} target="_blank" rel="noopener noreferrer"
                 title={t('history.viewOnX')}
                 className="mt-1 text-text-muted transition-colors hover:text-accent">
                <ExternalLink size={13} />
              </a>
            </div>
          ))}
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

      {/* Preview modal */}
      {previewPost && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
          onClick={() => setPreviewId(null)}
        >
          <div
            className="relative w-full max-w-[420px] mx-4"
            onClick={e => e.stopPropagation()}
          >
            <button
              onClick={() => setPreviewId(null)}
              className="absolute -top-3 -right-3 z-10 w-8 h-8 bg-bg border border-border rounded-full flex items-center justify-center text-text-muted hover:text-text hover:bg-bg-hover transition-colors shadow-lg"
            >
              <X size={16} />
            </button>
            <TweetPreview
              text={previewPost.text || ''}
              imageUrl={previewPost.image_path?.split(/[/\\]/).pop() ? api.uploadUrl(previewPost.image_path!.split(/[/\\]/).pop()!) : null}
              isVideo={isVideoFile(previewPost.image_path)}
              scheduledAt={previewPost.scheduled_at}
              profile={profile}
            />
          </div>
        </div>
      )}
    </div>
  )
}
