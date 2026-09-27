import { useState, useEffect, useCallback } from 'react'
import { X, ScanSearch, Loader2 } from 'lucide-react'
import { toast } from 'sonner'
import { PageHeader } from '@/components/PageHeader'
import { PostItem } from '@/components/PostItem'
import { TweetPreview } from '@/components/TweetPreview'
import { useConfirm } from '@/components/ConfirmModal'
import { useSettings } from '@/contexts/SettingsContext'
import * as api from '@/lib/api'
import type { Post } from '@/lib/api'
import { cn, toastResult, outcomeOf , isVideoFile } from '@/lib/utils'

type Tab = 'posted' | 'error'

export function History() {
  const { t } = useSettings()
  const confirm = useConfirm()
  const [tab, setTab] = useState<Tab>('posted')
  const [checking, setChecking] = useState(false)
  const [posts, setPosts] = useState<Post[]>([])
  const [loading, setLoading] = useState(true)
  const [previewPost, setPreviewPost] = useState<Post | null>(null)
  const [profile, setProfile] = useState<api.Profile | null>(null)

  useEffect(() => {
    api.fetchProfile().then(setProfile).catch(() => {})
  }, [])

  const load = useCallback(async (status: Tab) => {
    setLoading(true)
    try {
      const data = await api.fetchPosts(status)
      setPosts(data)
    } catch {
      toast.error(t('common.connectionError'))
    } finally {
      setLoading(false)
    }
  }, [t])

  useEffect(() => {
    load(tab)
  }, [tab, load])

  useEffect(() => {
    if (!previewPost) return
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setPreviewPost(null)
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [previewPost])

  /** Find posts X no longer has, and offer to drop them here too. */
  const checkOnX = async () => {
    setChecking(true)
    toast.info(t('history.checking'))
    try {
      const result = await api.checkPostsOnX()
      if (result.error) {
        toast.error(result.error)
        return
      }
      if (result.checked === 0) {
        toast.info(t('history.nothingToCheck'))
        return
      }
      if (result.truncated) toast.info(t('history.checkTruncated'))
      if (result.unknown > 0) {
        toast.info(`${result.unknown} ${t('history.checkInconclusive')}`)
      }
      if (result.missing.length === 0) {
        toast.success(t('history.allPresent'))
        return
      }
      const ok = await confirm({
        message: `${result.missing.length} ${t('history.missingFound')}`,
        danger: true,
      })
      if (!ok) return
      await Promise.all(result.missing.map(m => api.deletePost(m.id).catch(() => {})))
      toast.success(t('history.missingRemoved'))
      load(tab)
    } catch {
      toast.error(t('common.serverError'))
    } finally {
      setChecking(false)
    }
  }

  const handleAction = async (action: string, id: number) => {
    try {
      if (action === 'retry') {
        toast.info(t('history.retrying'))
        await api.retryPost(id)
        api.waitForPost(id)
          .then(post => toastResult(outcomeOf(post, 'posted'), t('post.published'), t))
          .catch(() => toast.error(t('common.serverError')))
          .finally(() => load(tab))
      } else if (action === 'duplicate') {
        await api.duplicatePost(id)
        toast.success(t('composer.duplicated'))
      } else if (action === 'delete') {
        if (!await confirm({ message: t('composer.confirmDelete'), danger: true })) return
        await api.deletePost(id)
        toast.success(t('composer.deleted'))
      } else if (action === 'delete-from-x') {
        if (!await confirm({ message: t('history.confirmDeleteFromX'), danger: true })) return
        toast.info(t('history.deletingFromX'))
        const r = await api.deleteFromX(id)
        if (r.success) {
          toast.success(r.already_deleted ? t('history.alreadyDeleted') : t('history.deletedFromX'))
        } else {
          toast.error(`${t('common.errorPrefix')} : ${r.error || t('common.unknownError')}`)
        }
      }
      load(tab)
    } catch {
      toast.error(t('common.serverError'))
    }
  }

  const thumbFile = (post: Post) => post.image_path?.split(/[/\\]/).pop()

  return (
    <div>
      <PageHeader title={t('history.title')} description={t('history.desc')} />

      {/* Reconcile with X: the app cannot know about a tweet deleted there. */}
      <div className="flex justify-end px-6 pt-4">
        <button
          onClick={checkOnX}
          disabled={checking}
          className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-text-secondary transition-colors hover:bg-bg-hover disabled:opacity-50"
        >
          {checking ? <Loader2 size={13} className="animate-spin" /> : <ScanSearch size={13} />}
          {t('history.checkOnX')}
        </button>
      </div>

      {/* Tabs */}
      <div className="flex border-b border-border">
        {([
          { id: 'posted' as Tab, label: t('history.published') },
          { id: 'error' as Tab, label: t('history.errors') },
        ]).map(ta => (
          <button
            key={ta.id}
            onClick={() => setTab(ta.id)}
            className={cn(
              'flex-1 py-3 text-sm font-medium text-center relative transition-colors',
              tab === ta.id ? 'text-text' : 'text-text-muted hover:text-text-secondary hover:bg-bg-hover'
            )}
          >
            {ta.label}
            {tab === ta.id && (
              <div className="absolute bottom-0 left-1/2 -translate-x-1/2 w-12 h-0.5 bg-accent rounded-full" />
            )}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="px-6 py-16 text-center text-sm text-text-muted">{t('common.loading')}</div>
      ) : posts.length === 0 ? (
        <div className="px-6 py-16 text-center">
          <p className="text-sm text-text-muted">
            {tab === 'posted' ? t('history.noPublished') : t('history.noErrors')}
          </p>
        </div>
      ) : (
        <div>
          {posts.map(p => (
            <PostItem
              key={p.id}
              post={p}
              actions={tab === 'error' ? ['retry', 'duplicate', 'delete'] : ['view-on-x', 'delete-from-x', 'duplicate']}
              onClick={setPreviewPost}
              onRetry={id => handleAction('retry', id)}
              onDuplicate={id => handleAction('duplicate', id)}
              onDelete={id => handleAction('delete', id)}
              onDeleteFromX={id => handleAction('delete-from-x', id)}
            />
          ))}
        </div>
      )}

      {/* Preview modal */}
      {previewPost && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
          onClick={() => setPreviewPost(null)}
        >
          <div
            className="relative w-full max-w-[420px] mx-4"
            onClick={e => e.stopPropagation()}
          >
            <button
              onClick={() => setPreviewPost(null)}
              className="absolute -top-3 -right-3 z-10 w-8 h-8 bg-bg border border-border rounded-full flex items-center justify-center text-text-muted hover:text-text hover:bg-bg-hover transition-colors shadow-lg"
            >
              <X size={16} />
            </button>
            <TweetPreview
              text={previewPost.text || ''}
              imageUrl={thumbFile(previewPost) ? api.uploadUrl(thumbFile(previewPost)!) : null}
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
