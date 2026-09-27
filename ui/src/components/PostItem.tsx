import { Pencil, Play, Copy, Trash2, RotateCcw, ExternalLink } from 'lucide-react'
import { MediaThumb } from '@/components/MediaThumb'
import type { Post } from '@/lib/api'
import { formatDate, timeFromNow, cn } from '@/lib/utils'
import { useSettings } from '@/contexts/SettingsContext'

interface PostItemProps {
  post: Post
  actions: ('edit' | 'post-now' | 'duplicate' | 'delete' | 'retry' | 'delete-from-x' | 'view-on-x')[]
  onClick?: (post: Post) => void
  onEdit?: (post: Post) => void
  onPostNow?: (id: number) => void
  onDuplicate?: (id: number) => void
  onDelete?: (id: number) => void
  onRetry?: (id: number) => void
  onDeleteFromX?: (id: number) => void
}

export function PostItem({ post, actions, onClick, onEdit, onPostNow, onDuplicate, onDelete, onRetry, onDeleteFromX }: PostItemProps) {
  const { t, locale } = useSettings()

  const statusConfig = {
    draft: { label: t('status.draft'), className: 'bg-bg-secondary text-text-secondary' },
    scheduled: { label: t('status.scheduled'), className: 'bg-accent-light text-accent' },
    scheduling: { label: t('status.scheduling'), className: 'bg-warning-light text-warning' },
    scheduled_on_x: { label: t('status.scheduled_on_x'), className: 'bg-success-light text-success' },
    posting: { label: t('status.posting'), className: 'bg-warning-light text-warning' },
    posted: { label: t('status.posted'), className: 'bg-success-light text-success' },
    error: { label: t('status.error'), className: 'bg-error-light text-error' },
  }

  const status = statusConfig[post.status as keyof typeof statusConfig] ?? statusConfig.draft
  const isLoading = post.status === 'posting'
  const date = post.scheduled_at ? formatDate(post.scheduled_at, locale) : formatDate(post.created_at, locale)
  const text = post.text || ''
  const thumbFile = post.image_path?.split(/[/\\]/).pop()

  return (
    <div
      className={cn(
        'flex items-start gap-4 px-6 py-4 border-b border-border hover:bg-bg-hover transition-colors',
        isLoading && 'opacity-60',
        onClick && 'cursor-pointer'
      )}
      onClick={() => onClick?.(post)}
    >
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-1.5">
          <span className={cn('text-[11px] font-medium px-2 py-0.5 rounded-full', status.className)}>
            {status.label}
          </span>
          <span className="text-xs text-text-muted">{date}</span>
          {post.status === 'scheduled' && post.scheduled_at && (
            <span className="text-xs text-accent font-medium">{timeFromNow(post.scheduled_at, locale)}</span>
          )}
          {post.retries_count > 0 && (
            <span className="text-xs text-text-muted">{t('post.retries')} : {post.retries_count}</span>
          )}
        </div>

        <p className="text-[13.5px] text-text leading-relaxed line-clamp-3 whitespace-pre-wrap break-words">
          {text || <em className="text-text-muted">{t('post.mediaOnly')}</em>}
        </p>

        {thumbFile && (
          <MediaThumb
            file={thumbFile}
            className="mt-2 w-24 h-16 object-cover rounded-md border border-border"
          />
        )}

        {post.error_message && (
          <div className="mt-2 text-xs text-error bg-error-light px-3 py-2 rounded-md">
            {post.error_message}
          </div>
        )}

        <div className="flex items-center gap-1 mt-2.5">
          {actions.includes('post-now') && (
            <button
              disabled={isLoading}
              onClick={e => { e.stopPropagation(); onPostNow?.(post.id) }}
              className="inline-flex items-center gap-1.5 text-xs text-text-secondary hover:text-text px-2 py-1 rounded hover:bg-bg-hover transition-colors disabled:opacity-40"
            >
              <Play size={13} /> {t('common.publish')}
            </button>
          )}
          {actions.includes('edit') && (
            <button
              disabled={isLoading}
              onClick={e => { e.stopPropagation(); onEdit?.(post) }}
              className="inline-flex items-center gap-1.5 text-xs text-text-secondary hover:text-text px-2 py-1 rounded hover:bg-bg-hover transition-colors disabled:opacity-40"
            >
              <Pencil size={13} /> {t('common.edit')}
            </button>
          )}
          {actions.includes('retry') && (
            <button
              disabled={isLoading}
              onClick={e => { e.stopPropagation(); onRetry?.(post.id) }}
              className="inline-flex items-center gap-1.5 text-xs text-success hover:text-success px-2 py-1 rounded hover:bg-success-light transition-colors disabled:opacity-40"
            >
              <RotateCcw size={13} /> {t('common.retry')}
            </button>
          )}
          {actions.includes('duplicate') && (
            <button
              disabled={isLoading}
              onClick={e => { e.stopPropagation(); onDuplicate?.(post.id) }}
              className="inline-flex items-center gap-1.5 text-xs text-text-secondary hover:text-text px-2 py-1 rounded hover:bg-bg-hover transition-colors disabled:opacity-40"
            >
              <Copy size={13} /> {t('common.duplicate')}
            </button>
          )}
          {actions.includes('view-on-x') && post.tweet_url && (
            <a
              href={post.tweet_url}
              target="_blank"
              rel="noopener noreferrer"
              onClick={e => e.stopPropagation()}
              className="inline-flex items-center gap-1.5 text-xs text-accent hover:text-accent px-2 py-1 rounded hover:bg-accent-light transition-colors"
            >
              <ExternalLink size={13} /> {t('history.viewOnX')}
            </a>
          )}
          {actions.includes('delete-from-x') && (post.tweet_url || (post.status === 'scheduled_on_x' && post.text)) && (
            <button
              disabled={isLoading}
              onClick={e => { e.stopPropagation(); onDeleteFromX?.(post.id) }}
              className="inline-flex items-center gap-1.5 text-xs text-error/70 hover:text-error px-2 py-1 rounded hover:bg-error-light transition-colors disabled:opacity-40"
            >
              <Trash2 size={13} /> {t('history.deleteFromX')}
            </button>
          )}
          {actions.includes('delete') && (
            <button
              onClick={e => { e.stopPropagation(); onDelete?.(post.id) }}
              className="inline-flex items-center gap-1.5 text-xs text-error/70 hover:text-error px-2 py-1 rounded hover:bg-error-light transition-colors"
            >
              <Trash2 size={13} /> {t('common.delete')}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
