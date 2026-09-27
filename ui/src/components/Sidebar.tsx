import {
  PenSquare,
  CalendarClock,
  CalendarDays,
  History,
  ScrollText,
  Settings,
  User,
  Wifi,
  WifiOff,
  Lock,
  Info,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import { cn } from '@/lib/utils'
import { useSettings } from '@/contexts/SettingsContext'
import type { TranslationKey } from '@/lib/i18n'

type Page = 'composer' | 'schedule' | 'calendar' | 'history' | 'logs' | 'settings' | 'profile' | 'about'

interface SidebarProps {
  activePage: Page
  onNavigate: (page: Page) => void
}

const nav: { id: Page; labelKey: TranslationKey; icon: typeof PenSquare }[] = [
  { id: 'composer', labelKey: 'nav.composer', icon: PenSquare },
  { id: 'schedule', labelKey: 'nav.schedule', icon: CalendarClock },
  { id: 'calendar', labelKey: 'nav.calendar', icon: CalendarDays },
  { id: 'history', labelKey: 'nav.history', icon: History },
  { id: 'logs', labelKey: 'nav.logs', icon: ScrollText },
  { id: 'profile', labelKey: 'nav.profile', icon: User },
  { id: 'settings', labelKey: 'nav.settings', icon: Settings },
  { id: 'about', labelKey: 'nav.about', icon: Info },
]

export function Sidebar({ activePage, onNavigate }: SidebarProps) {
  const { t, configured, theme } = useSettings()
  const [connected, setConnected] = useState<boolean | null>(null)

  useEffect(() => {
    const check = () => {
      fetch('/api/profile')
        .then(r => r.json())
        .then(d => setConnected(!!d.username))
        .catch(() => setConnected(false))
    }
    check()
    const interval = setInterval(check, 30000)
    return () => clearInterval(interval)
  }, [])

  return (
    <aside className="w-60 h-screen flex flex-col border-r border-border bg-bg-secondary shrink-0">
      {/* Logo */}
      <div className="px-5 py-5 flex items-center gap-1.5">
        <img src={theme === 'dark' ? '/logo blanc.png' : '/logo noir.png'} alt="Logo" className="w-[46px] h-[46px] rounded-lg object-contain" />
        <span className="font-semibold text-[15px] text-text-secondary">Post Management</span>
      </div>

      {/* Nav */}
      <nav className="flex-1 px-3 space-y-0.5">
        {nav.map(item => {
          const Icon = item.icon
          const active = activePage === item.id
          const locked = !configured && item.id !== 'settings' && item.id !== 'about'
          return (
            <button
              key={item.id}
              onClick={() => !locked && onNavigate(item.id)}
              disabled={locked}
              title={locked ? t('sidebar.locked') : undefined}
              className={cn(
                'w-full flex items-center gap-3 px-3 py-2 rounded-lg text-[13.5px] font-medium transition-colors text-left',
                locked
                  ? 'text-text-muted opacity-50 cursor-not-allowed'
                  : active
                    ? 'bg-bg text-text shadow-sm ring-1 ring-border'
                    : 'text-text-secondary hover:bg-bg hover:text-text'
              )}
            >
              <Icon size={18} strokeWidth={active ? 2 : 1.5} />
              {t(item.labelKey)}
              {locked && <Lock size={13} className="ml-auto text-text-muted" />}
            </button>
          )
        })}
      </nav>

      {/* Status */}
      <div className="px-4 py-3 border-t border-border/60">
        <div className="flex items-center gap-2 text-xs text-text-muted">
          {connected === null ? (
            <span className="text-text-muted">{t('sidebar.loading')}</span>
          ) : connected ? (
            <>
              <Wifi size={14} className="text-success" />
              <span className="text-success">{t('sidebar.connected')}</span>
            </>
          ) : (
            <>
              <WifiOff size={14} className="text-text-muted" />
              <span>{t('sidebar.disconnected')}</span>
            </>
          )}
        </div>
      </div>

      {/* Author */}
      <div className="px-4 py-3 border-t border-border">
        <div className="flex items-center justify-between">
          <p className="text-[10px] text-text-muted leading-relaxed">
            Made by <span className="font-medium text-text-secondary">Loic Meyer</span>
          </p>
          <span className="text-[10px] text-text-muted font-mono">v{__APP_VERSION__}</span>
        </div>
        <div className="flex items-center gap-3 mt-1.5">
          <a
            href="https://github.com/LoicmeyerMMI"
            target="_blank"
            rel="noopener noreferrer"
            className="text-text-muted hover:text-text transition-colors"
            title="GitHub"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z"/></svg>
          </a>
          <a
            href="https://www.linkedin.com/in/loic-meyer/"
            target="_blank"
            rel="noopener noreferrer"
            className="text-text-muted hover:text-[#0A66C2] transition-colors"
            title="LinkedIn"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 01-2.063-2.065 2.064 2.064 0 112.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/></svg>
          </a>
        </div>
      </div>
    </aside>
  )
}
