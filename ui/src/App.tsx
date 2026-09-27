import { useState, useEffect, lazy, Suspense } from 'react'
import { Toaster } from 'sonner'
import { SettingsProvider, useSettings } from './contexts/SettingsContext'
import { ComposerProvider } from './contexts/ComposerContext'
import { NavigationProvider, type Page } from './contexts/NavigationContext'
import { ConfirmProvider } from './components/ConfirmModal'
import { SetupWizard } from './components/SetupWizard'
import { Sidebar } from './components/Sidebar'
import { Composer } from './pages/Composer'
import { Schedule } from './pages/Schedule'
import { Calendar } from './pages/Calendar'
import { History } from './pages/History'
import { Logs } from './pages/Logs'
import { Settings } from './pages/Settings'
import { About } from './pages/About'
import { Loader2, Coffee } from 'lucide-react'
import * as api from './lib/api'

// Profile is the only page pulling in the charting library: load it on demand
// so it stays out of the bundle the app parses at startup.
const Profile = lazy(() => import('./pages/Profile').then(m => ({ default: m.Profile })))

const BMC_URL = 'https://buymeacoffee.com/loicmeyer'

function PageLoader() {
  return (
    <div className="flex h-full items-center justify-center">
      <Loader2 size={20} className="animate-spin text-text-muted" />
    </div>
  )
}

function AppContent() {
  const [page, setPage] = useState<Page>('composer')
  const { configured, recheckConfig, recheckConnection } = useSettings()
  const [setupComplete, setSetupComplete] = useState<boolean | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)

  // Load setupComplete from server preferences (localStorage doesn't persist in pywebview)
  useEffect(() => {
    api.fetchPreferences().then(prefs => {
      setSetupComplete(prefs.setupComplete === 'true')
    }).catch(() => {
      setSetupComplete(false)
    })
  }, [])

  // Force the settings page once, when the config turns out to be invalid.
  // This is a one-shot redirect, not derived state: the user may navigate away
  // again afterwards, so it cannot be computed during render.
  useEffect(() => {
    if (configured === false && setupComplete === true) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setPage('settings')
    }
  }, [configured, setupComplete])

  const handleNavigate = (p: Page) => {
    if (!configured && p !== 'settings' && p !== 'about') return
    setPage(p)
  }

  if (configured === null || setupComplete === null) {
    return (
      <div className="flex h-screen items-center justify-center bg-bg">
        <Loader2 size={24} className="animate-spin text-text-muted" />
      </div>
    )
  }

  const handleSetupComplete = async () => {
    setSetupComplete(true)
    // Persist to server so it survives pywebview restarts
    api.savePreferences({ setupComplete: 'true' }).catch(() => {})
    // Refresh app state after wizard completes
    await Promise.all([recheckConfig(), recheckConnection()])
    // Force remount of pages so Composer refreshes
    setPage('composer')
    setRefreshKey(k => k + 1)
  }

  const renderPage = () => {
    switch (page) {
      case 'composer': return <Composer />
      case 'schedule': return <Schedule />
      case 'calendar': return <Calendar />
      case 'history': return <History />
      case 'logs': return <Logs />
      case 'settings': return <Settings />
      case 'profile': return <Profile />
      case 'about': return <About />
    }
  }

  return (
    <NavigationProvider onNavigate={handleNavigate}>
    <div className="flex h-screen overflow-hidden bg-bg">
      {/* Setup wizard */}
      {setupComplete === false && <SetupWizard onComplete={handleSetupComplete} />}

      <Sidebar activePage={page} onNavigate={handleNavigate} />
      <main key={refreshKey} className="flex-1 overflow-y-auto">
        <Suspense fallback={<PageLoader />}>{renderPage()}</Suspense>
      </main>
      {/* Buy me a coffee */}
      <a
        href={BMC_URL}
        target="_blank"
        rel="noopener noreferrer"
        className="fixed bottom-5 right-5 z-40 flex items-center gap-2 px-3.5 py-2 bg-[#111827]/80 dark:bg-white/90 text-white/90 dark:text-[#111827] rounded-full shadow-[0_2px_8px_rgba(0,0,0,0.1)] dark:shadow-[0_2px_8px_rgba(255,255,255,0.15)] hover:bg-[#111827] dark:hover:bg-white hover:shadow-[0_4px_14px_rgba(0,0,0,0.2)] dark:hover:shadow-[0_4px_14px_rgba(255,255,255,0.25)] hover:scale-105 transition-all text-[11px] font-medium opacity-70 hover:opacity-100"
      >
        <Coffee size={16} strokeWidth={2.5} />
        Buy me a coffee
      </a>

      <Toaster
        position="bottom-center"
        toastOptions={{
          style: {
            background: '#111827',
            color: '#fff',
            border: 'none',
            borderRadius: '8px',
            fontSize: '13px',
          },
        }}
      />
    </div>
    </NavigationProvider>
  )
}

export default function App() {
  return (
    <SettingsProvider>
    <ComposerProvider>
    <ConfirmProvider>
      <AppContent />
    </ConfirmProvider>
    </ComposerProvider>
    </SettingsProvider>
  )
}
