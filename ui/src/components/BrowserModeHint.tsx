import { useState, useEffect, useCallback } from 'react'
import { EyeOff, Monitor, X, ChevronRight } from 'lucide-react'
import { useSettings } from '@/contexts/SettingsContext'
import { useNavigation } from '@/contexts/NavigationContext'
import * as api from '@/lib/api'

const SEEN_KEY = 'browserHintSeen'
const BROWSER_SECTION = 'browser-mode'

/**
 * Tells people, once, that the browser can work out of sight.
 *
 * It reads the current mode rather than guessing: already invisible and it just
 * says so, still visible and it offers to switch in one click. Dismissing it is
 * remembered server-side, so it does not come back on the next launch.
 */
export function BrowserModeHint() {
  const { t } = useSettings()
  const { goTo } = useNavigation()
  const [visible, setVisible] = useState(false)
  const [headless, setHeadless] = useState(true)

  useEffect(() => {
    let cancelled = false
    Promise.all([api.fetchPreferences(), api.fetchEnvSettings()])
      .then(([prefs, env]) => {
        if (cancelled) return
        // Only worth showing once the app is actually set up.
        if (prefs[SEEN_KEY] === 'true' || prefs.setupComplete !== 'true') return
        setHeadless(env.HEADLESS !== 'false')
        setVisible(true)
      })
      .catch(() => {})
    return () => { cancelled = true }
  }, [])

  const dismiss = useCallback(() => {
    setVisible(false)
    api.savePreferences({ [SEEN_KEY]: 'true' }).catch(() => {})
  }, [])

  // Clicking the hint takes you straight to the switch, rather than leaving you
  // to hunt for it in Settings.
  const openSetting = useCallback(() => {
    goTo('settings', BROWSER_SECTION)
  }, [goTo])

  if (!visible) return null

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={openSetting}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openSetting() } }}
      className="mx-6 mt-5 flex cursor-pointer items-start gap-3 rounded-lg border border-accent/30 bg-accent-light/60 px-4 py-3 text-left transition-colors hover:border-accent/60 hover:bg-accent-light dark:bg-accent/10 dark:hover:bg-accent/20"
    >
      <div className="mt-0.5 shrink-0 text-accent">
        {headless ? <EyeOff size={16} /> : <Monitor size={16} />}
      </div>

      <div className="min-w-0 flex-1">
        <p className="text-xs font-semibold text-text">
          {headless ? t('hint.browserInvisibleTitle') : t('hint.browserVisibleTitle')}
        </p>
        <p className="mt-0.5 text-[11px] leading-relaxed text-text-secondary">
          {headless ? t('hint.browserInvisibleBody') : t('hint.browserVisibleBody')}
        </p>
        <span className="mt-1.5 inline-flex items-center gap-0.5 text-[11px] font-medium text-accent">
          {t('hint.openSetting')}
          <ChevronRight size={12} />
        </span>
      </div>

      <button
        onClick={e => { e.stopPropagation(); dismiss() }}
        className="shrink-0 rounded-md p-1 text-text-muted transition-colors hover:bg-bg-hover hover:text-text"
        title={t('hint.dismiss')}
      >
        <X size={14} />
      </button>
    </div>
  )
}
