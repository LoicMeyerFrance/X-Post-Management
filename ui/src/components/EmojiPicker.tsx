import { useState, useRef, useEffect, lazy, Suspense } from 'react'
import { Smile, Loader2 } from 'lucide-react'
import { useSettings } from '@/contexts/SettingsContext'

// emoji-mart ships a large emoji database. Loading it only when the picker is
// first opened keeps it out of the initial bundle.
const Picker = lazy(() => import('@emoji-mart/react'))

type EmojiData = Record<string, unknown>

let dataPromise: Promise<{ data: EmojiData; i18n: Record<string, EmojiData> }> | null = null

function loadEmojiData() {
  if (!dataPromise) {
    dataPromise = Promise.all([
      import('@emoji-mart/data'),
      import('@emoji-mart/data/i18n/fr.json'),
    ]).then(([data, fr]) => ({
      data: data.default as EmojiData,
      i18n: { fr: fr.default as EmojiData },
    }))
  }
  return dataPromise
}

interface EmojiPickerProps {
  onSelect: (emoji: string) => void
}

export function EmojiPicker({ onSelect }: EmojiPickerProps) {
  const { theme, locale, t } = useSettings()
  const [open, setOpen] = useState(false)
  const [openUp, setOpenUp] = useState(true)
  const [emoji, setEmoji] = useState<Awaited<ReturnType<typeof loadEmojiData>> | null>(null)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    if (open) document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [open])

  useEffect(() => {
    if (!open || emoji) return
    let cancelled = false
    loadEmojiData().then(loaded => { if (!cancelled) setEmoji(loaded) })
    return () => { cancelled = true }
  }, [open, emoji])

  const toggle = () => {
    if (!open && ref.current) {
      const rect = ref.current.getBoundingClientRect()
      // 420px = approximate picker height
      setOpenUp(rect.top > 420)
    }
    setOpen(!open)
  }

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={toggle}
        className="w-8 h-8 flex items-center justify-center rounded-md text-accent hover:bg-accent-light transition-colors"
        title={t('composer.addEmoji')}
      >
        <Smile size={18} />
      </button>
      {open && (
        <div className={`absolute left-0 z-50 shadow-lg rounded-xl overflow-hidden max-h-[380px] ${openUp ? 'bottom-10' : 'top-10'}`}>
          {emoji ? (
            <Suspense fallback={<PickerFallback />}>
              <Picker
                data={emoji.data}
                onEmojiSelect={(selected: { native: string }) => {
                  onSelect(selected.native)
                  setOpen(false)
                }}
                theme={theme === 'dark' ? 'dark' : 'light'}
                locale={locale}
                i18n={emoji.i18n[locale]}
                previewPosition="none"
                skinTonePosition="none"
                searchPosition="top"
                perLine={7}
                maxFrequentRows={1}
                navPosition="bottom"
                dynamicWidth={false}
              />
            </Suspense>
          ) : (
            <PickerFallback />
          )}
        </div>
      )}
    </div>
  )
}

function PickerFallback() {
  return (
    <div className="w-[280px] h-[380px] flex items-center justify-center bg-bg-secondary border border-border rounded-xl">
      <Loader2 size={18} className="animate-spin text-text-muted" />
    </div>
  )
}
