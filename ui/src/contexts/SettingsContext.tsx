import { createContext, useContext, useState, useCallback, useEffect, type ReactNode } from 'react'
import { getTranslation, type Locale, type TranslationKey } from '@/lib/i18n'
import * as api from '@/lib/api'

type Theme = 'light' | 'dark'

interface SettingsState {
  locale: Locale
  setLocale: (l: Locale) => void
  theme: Theme
  setTheme: (t: Theme) => void
  t: (key: TranslationKey) => string
  configured: boolean | null
  recheckConfig: () => Promise<void>
  xConnected: boolean | null
  checkingConnection: boolean
  recheckConnection: () => Promise<void>
}

const SettingsContext = createContext<SettingsState | null>(null)

export function SettingsProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleRaw] = useState<Locale>(() => (localStorage.getItem('locale') as Locale) || 'en')
  const [theme, setThemeRaw] = useState<Theme>(() => (localStorage.getItem('theme') as Theme) || 'light')
  const [configured, setConfigured] = useState<boolean | null>(null)

  // Load preferences from server on mount (persists across pywebview sessions)
  useEffect(() => {
    api.fetchPreferences().then(prefs => {
      if (prefs.locale && (prefs.locale === 'fr' || prefs.locale === 'en')) {
        setLocaleRaw(prefs.locale)
        localStorage.setItem('locale', prefs.locale)
      }
      if (prefs.theme && (prefs.theme === 'light' || prefs.theme === 'dark')) {
        setThemeRaw(prefs.theme as Theme)
        localStorage.setItem('theme', prefs.theme)
      }
    }).catch(() => {})
  }, [])

  const setLocale = useCallback((l: Locale) => {
    setLocaleRaw(l)
    localStorage.setItem('locale', l)
    api.savePreferences({ locale: l }).catch(() => {})
  }, [])

  const setTheme = useCallback((t: Theme) => {
    setThemeRaw(t)
    localStorage.setItem('theme', t)
    api.savePreferences({ theme: t }).catch(() => {})
  }, [])

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark')
  }, [theme])

  const t = useCallback((key: TranslationKey) => getTranslation(key, locale), [locale])

  const recheckConfig = useCallback(async () => {
    try {
      const res = await fetch('/api/settings/env')
      const data: Record<string, string> = await res.json()
      setConfigured(!!(data.X_USERNAME && data.X_PASSWORD))
    } catch {
      setConfigured(false)
    }
  }, [])

  const [xConnected, setXConnected] = useState<boolean | null>(null)
  const [checkingConnection, setCheckingConnection] = useState(false)

  const recheckConnection = useCallback(async () => {
    setCheckingConnection(true)
    try {
      const result = await api.fetchConnectionStatus()
      setXConnected(result.connected)
    } catch {
      setXConnected(null)
    } finally {
      setCheckingConnection(false)
    }
  }, [])

  // Check config + connection state on mount, then re-check config every 30s
  useEffect(() => {
    recheckConfig()
    recheckConnection()
    const interval = setInterval(recheckConfig, 30000)
    return () => clearInterval(interval)
  }, [recheckConfig, recheckConnection])

  return (
    <SettingsContext.Provider value={{ locale, setLocale, theme, setTheme, t, configured, recheckConfig, xConnected, checkingConnection, recheckConnection }}>
      {children}
    </SettingsContext.Provider>
  )
}

export function useSettings() {
  const ctx = useContext(SettingsContext)
  if (!ctx) throw new Error('useSettings must be used within SettingsProvider')
  return ctx
}
