import { Play } from 'lucide-react'
import { uploadUrl } from '@/lib/api'
import { isVideoFile } from '@/lib/utils'

interface MediaThumbProps {
  /** File name as stored in data/uploads. */
  file: string
  className?: string
}

/**
 * A stored attachment, shown as itself.
 *
 * Every list used to render the attachment with an <img>, which left a broken
 * image wherever the post carried a video. A video gets a real <video> here,
 * with its first frame as the thumbnail and a play badge so it reads as one at
 * a glance.
 */
export function MediaThumb({ file, className = '' }: MediaThumbProps) {
  const src = uploadUrl(file)

  if (!isVideoFile(file)) {
    return <img src={src} alt="" className={className} loading="lazy" />
  }

  return (
    <div className="relative inline-block">
      {/* #t=0.1 nudges the browser to paint a frame instead of a black box. */}
      <video src={`${src}#t=0.1`} className={className} muted preload="metadata" />
      <span className="pointer-events-none absolute inset-0 flex items-center justify-center">
        <span className="flex h-5 w-5 items-center justify-center rounded-full bg-black/60">
          <Play size={10} className="ml-px fill-white text-white" />
        </span>
      </span>
    </div>
  )
}
