import { MessageCircle, Repeat2, Heart, Bookmark, Share } from 'lucide-react'
import type { Profile } from '@/lib/api'
import { profilePictureUrl } from '@/lib/api'
import { formatDate } from '@/lib/utils'
import { useSettings } from '@/contexts/SettingsContext'

interface TweetPreviewProps {
  text: string
  imageUrl?: string | null
  isVideo?: boolean
  scheduledAt?: string | null
  profile: Profile | null
}

export function TweetPreview({ text, imageUrl, isVideo, scheduledAt, profile }: TweetPreviewProps) {
  const { t, locale } = useSettings()

  const previewDate = () => {
    if (scheduledAt) return formatDate(scheduledAt, locale)
    const now = new Date()
    const loc = locale === 'fr' ? 'fr-FR' : 'en-US'
    return now.toLocaleString(loc, { hour: '2-digit', minute: '2-digit' }) + ' · ' +
      now.toLocaleDateString(loc, { day: 'numeric', month: 'short', year: 'numeric' })
  }

  return (
    <div className="bg-bg border border-border rounded-2xl overflow-hidden">
      {/* Tweet header */}
      <div className="px-4 pt-3 pb-0">
        <div className="flex items-start gap-2.5">
          <img
            src={profilePictureUrl()}
            alt=""
            className="w-10 h-10 rounded-full bg-bg-secondary object-cover shrink-0"
            onError={e => {
              const el = e.target as HTMLImageElement
              el.style.display = 'none'
            }}
          />
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-1">
              <span className="font-bold text-[15px] text-text truncate">{profile?.display_name || t('preview.myAccount')}</span>
            </div>
            <span className="text-[13px] text-text-muted">@{profile?.username || 'handle'}</span>
          </div>
          <svg viewBox="0 0 24 24" className="w-5 h-5 text-text-muted shrink-0"><circle cx="5" cy="12" r="1.5" fill="currentColor"/><circle cx="12" cy="12" r="1.5" fill="currentColor"/><circle cx="19" cy="12" r="1.5" fill="currentColor"/></svg>
        </div>
      </div>

      {/* Tweet body */}
      <div className="px-4 pt-2 pb-3">
        <p className="text-[15px] leading-[1.45] whitespace-pre-wrap break-words text-text">
          {text || <span className="text-text-muted italic">{t('preview.textPlaceholder')}</span>}
        </p>
        {imageUrl && (isVideo ? (
          <video
            src={imageUrl}
            controls
            className="mt-3 w-full max-h-[280px] rounded-2xl border border-border"
          />
        ) : (
          <img
            src={imageUrl}
            alt=""
            className="mt-3 w-full max-h-[280px] object-cover rounded-2xl border border-border"
          />
        ))}
      </div>

      {/* Tweet date */}
      <div className="px-4 py-3 border-t border-border">
        <span className="text-[13px] text-text-muted">{previewDate()}</span>
      </div>

      {/* Action buttons row */}
      <div className="mx-4 py-2 border-t border-border flex items-center justify-around">
        <button className="group flex items-center gap-1 p-2 rounded-full hover:bg-accent-light transition-colors" type="button" tabIndex={-1}>
          <MessageCircle size={18} className="text-text-muted group-hover:text-accent" />
        </button>
        <button className="group flex items-center gap-1 p-2 rounded-full hover:bg-green-50 transition-colors" type="button" tabIndex={-1}>
          <Repeat2 size={18} className="text-text-muted group-hover:text-green-600" />
        </button>
        <button className="group flex items-center gap-1 p-2 rounded-full hover:bg-red-50 transition-colors" type="button" tabIndex={-1}>
          <Heart size={18} className="text-text-muted group-hover:text-red-500" />
        </button>
        <div className="flex items-center gap-1">
          <button className="group p-2 rounded-full hover:bg-accent-light transition-colors" type="button" tabIndex={-1}>
            <Bookmark size={18} className="text-text-muted group-hover:text-accent" />
          </button>
          <button className="group p-2 rounded-full hover:bg-accent-light transition-colors" type="button" tabIndex={-1}>
            <Share size={18} className="text-text-muted group-hover:text-accent" />
          </button>
        </div>
      </div>

      {/* Status line */}
      <div className="px-4 py-2 bg-bg-secondary border-t border-border">
        <span className="text-[12px] text-text-muted">
          {scheduledAt ? `📅 ${t('preview.scheduledFor')} ${formatDate(scheduledAt, locale)}` : `✓ ${t('preview.ready')}`}
        </span>
      </div>
    </div>
  )
}
