import { useState } from 'react'
import { Loader2, ArrowRight, Lock, LogIn, CheckCircle2, User, Globe, AlertCircle } from 'lucide-react'
import { toast } from 'sonner'
import { useSettings } from '@/contexts/SettingsContext'
import * as api from '@/lib/api'

interface SetupWizardProps {
  onComplete: () => void
}

const STEP_KEYS = ['setup.stepWelcome', 'setup.stepCredentials', 'setup.stepConnect', 'setup.stepProfile'] as const

export function SetupWizard({ onComplete }: SetupWizardProps) {
  const { t, locale, setLocale, configured, recheckConfig, xConnected, recheckConnection } = useSettings()

  // App shows a spinner until `configured` resolves, so the right starting step
  // is known during the first render - no effect needed to jump ahead.
  const [step, setStep] = useState(() => {
    if (configured !== true) return 1
    return xConnected === true ? 4 : 3
  })
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [savingCredentials, setSavingCredentials] = useState(false)
  const [connecting, setConnecting] = useState(false)
  const [connectError, setConnectError] = useState<string | null>(null)
  const [rateLimited, setRateLimited] = useState(false)
  const [fetchingProfile, setFetchingProfile] = useState(false)
  const [profileResult, setProfileResult] = useState<{ display_name: string; username: string } | null>(null)

  const handleSaveCredentials = async () => {
    if (!username.trim() || !password.trim()) {
      toast.error(t('setup.fillBoth'))
      return
    }
    setSavingCredentials(true)
    try {
      const existing = await api.fetchEnvSettings()
      const merged = { ...existing, X_USERNAME: username.trim(), X_PASSWORD: password.trim() }
      const result = await api.saveEnvSettings(merged)
      if (result.success) {
        toast.success(t('setup.credentialsSaved'))
        await recheckConfig()
        setStep(3)
      } else {
        toast.error(result.error || t('settings.errorUnknown'))
      }
    } catch {
      toast.error(t('common.serverError'))
    } finally {
      setSavingCredentials(false)
    }
  }

  const handleConnect = async () => {
    setConnecting(true)
    setConnectError(null)
    setRateLimited(false)
    try {
      const result = await api.connectX()
      if (result.success) {
        toast.success(t('setup.connected'))
        await recheckConnection()
        setStep(4)
      } else {
        setConnectError(result.error || t('settings.errorUnknown'))
        setRateLimited(!!result.rate_limited)
      }
    } catch {
      setConnectError(t('common.serverError'))
    } finally {
      setConnecting(false)
    }
  }

  const setHeadlessMode = async () => {
    try {
      const existing = await api.fetchEnvSettings()
      if (existing.HEADLESS !== 'true') {
        await api.saveEnvSettings({ ...existing, HEADLESS: 'true' })
      }
    } catch { /* ignore */ }
  }

  const handleFetchProfile = async () => {
    setFetchingProfile(true)
    try {
      // Set browser to invisible before fetching profile
      await setHeadlessMode()
      const result = await api.fetchProfileFromX()
      if (result.success) {
        setProfileResult({ display_name: result.display_name || '', username: result.username || '' })
        toast.success(t('setup.profileSuccess'))
      } else {
        toast.error(result.error || t('settings.errorUnknown'))
      }
    } catch {
      toast.error(t('common.serverError'))
    } finally {
      setFetchingProfile(false)
    }
  }

  const handleFinish = async () => {
    // Ensure headless mode is set by default
    await setHeadlessMode()
    onComplete()
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-gradient-to-br from-black/40 via-black/50 to-accent/10 backdrop-blur-sm">
      <div className="w-full max-w-lg mx-4 bg-bg border border-border rounded-2xl shadow-2xl overflow-hidden">
        {/* Progress indicator */}
        <div className="px-8 pt-6 pb-2">
          <div className="flex items-center justify-between">
            {STEP_KEYS.map((key, i) => {
              const stepNum = i + 1
              const isActive = step === stepNum
              const isDone = step > stepNum
              return (
                <div key={key} className="flex flex-col items-center flex-1">
                  <div className={`w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold transition-all ${
                    isDone ? 'bg-green-500 text-white' :
                    isActive ? 'bg-accent text-white' :
                    'bg-bg-secondary text-text-muted'
                  }`}>
                    {isDone ? <CheckCircle2 size={16} /> : stepNum}
                  </div>
                  <span className={`mt-1.5 text-[10px] font-medium ${isActive ? 'text-accent' : 'text-text-muted'}`}>
                    {t(key)}
                  </span>
                </div>
              )
            })}
          </div>
          {/* Progress line */}
          <div className="mt-2 mx-8 h-0.5 bg-bg-secondary rounded-full overflow-hidden">
            <div
              className="h-full bg-accent transition-all duration-300"
              style={{ width: `${((step - 1) / 3) * 100}%` }}
            />
          </div>
        </div>

        {/* Step content */}
        <div className="px-8 py-6">
          {/* Step 1: Welcome */}
          {step === 1 && (
            <div className="text-center">
              {/* Language toggle — small globe icon */}
              <div className="flex justify-end -mt-2 mb-6">
                <button
                  onClick={() => setLocale(locale === 'en' ? 'fr' : 'en')}
                  className="inline-flex items-center gap-1.5 px-2 py-1 rounded-md text-[11px] font-medium text-text-muted hover:text-text hover:bg-bg-hover transition-colors"
                  title={locale === 'en' ? 'Français' : 'English'}
                >
                  <Globe size={13} />
                  {locale === 'en' ? 'FR' : 'EN'}
                </button>
              </div>

              {/* Logo — larger, with gradient glow */}
              <div className="relative w-[88px] h-[88px] mx-auto mb-5">
                <div className="absolute inset-0 rounded-2xl bg-gradient-to-br from-accent/20 to-accent/5 blur-lg scale-110" />
                <img
                  src="/logo.png"
                  alt="Logo"
                  className="relative w-full h-full rounded-2xl shadow-lg ring-1 ring-black/[0.08] dark:ring-white/[0.08]"
                />
              </div>

              <h2 className="text-xl font-bold text-text mb-1.5">{t('setup.welcomeTitle')}</h2>
              <p className="text-sm text-text-muted mb-8 max-w-[280px] mx-auto leading-relaxed">{t('setup.welcomeDesc')}</p>
              <button
                onClick={() => setStep(2)}
                className="inline-flex items-center gap-2 px-8 py-2.5 bg-accent text-white font-semibold rounded-lg hover:bg-accent-hover transition-all shadow-md shadow-accent/25 hover:shadow-lg hover:shadow-accent/30"
              >
                {t('setup.getStarted')}
                <ArrowRight size={16} />
              </button>
            </div>
          )}

          {/* Step 2: Credentials */}
          {step === 2 && (
            <div>
              <h2 className="text-lg font-bold text-text mb-1">{t('setup.credentialsTitle')}</h2>
              <p className="text-sm text-text-muted mb-4">{t('setup.credentialsDesc')}</p>
              <div className="space-y-3">
                <div>
                  <label className="block text-xs font-medium text-text mb-1">{t('setup.username')}</label>
                  <input
                    type="text"
                    value={username}
                    onChange={e => {
                      let val = e.target.value
                      if (val.startsWith('@')) val = val.slice(1)
                      setUsername(val)
                    }}
                    placeholder={t('setup.usernamePlaceholder')}
                    className="w-full px-3 py-2 text-sm border border-border rounded-lg focus:outline-none focus:border-accent bg-bg text-text"
                  />
                  {username.includes('@') && (
                    <p className="mt-1 text-[11px] text-amber-600 dark:text-amber-400">
                      {t('settings.usernameNotEmail')}
                    </p>
                  )}
                </div>
                <div>
                  <label className="block text-xs font-medium text-text mb-1">{t('setup.password')}</label>
                  <input
                    type="password"
                    value={password}
                    onChange={e => setPassword(e.target.value)}
                    className="w-full px-3 py-2 text-sm border border-border rounded-lg focus:outline-none focus:border-accent bg-bg text-text"
                  />
                </div>
              </div>
              <div className="flex items-start gap-2 mt-4 p-3 bg-bg-secondary rounded-lg">
                <Lock size={14} className="text-text-muted shrink-0 mt-0.5" />
                <p className="text-[11px] text-text-muted">{t('setup.securityNote')}</p>
              </div>
              <div className="mt-6 flex justify-end">
                <button
                  onClick={handleSaveCredentials}
                  disabled={!username.trim() || !password.trim() || savingCredentials}
                  className="inline-flex items-center gap-2 px-5 py-2 bg-accent text-white font-medium text-sm rounded-lg hover:bg-accent-hover transition-colors disabled:opacity-50"
                >
                  {savingCredentials ? <Loader2 size={14} className="animate-spin" /> : null}
                  {t('setup.continue')}
                  {!savingCredentials && <ArrowRight size={14} />}
                </button>
              </div>
            </div>
          )}

          {/* Step 3: Connect to X */}
          {step === 3 && (
            <div className="text-center">
              <h2 className="text-lg font-bold text-text mb-1">{t('setup.connectTitle')}</h2>
              <p className="text-sm text-text-muted mb-2">{t('setup.connectDesc')}</p>
              <p className="text-xs text-text-muted/70 mb-6">{t('setup.connectNote')}</p>

              {xConnected === true ? (
                <div className="flex flex-col items-center gap-3 mb-6">
                  <div className="w-12 h-12 rounded-full bg-green-100 dark:bg-green-950/30 flex items-center justify-center">
                    <CheckCircle2 size={24} className="text-green-600 dark:text-green-400" />
                  </div>
                  <span className="text-sm font-medium text-green-700 dark:text-green-400">{t('setup.connected')}</span>
                </div>
              ) : (
                <div className="flex flex-col items-center gap-3 mb-6">
                  <button
                    onClick={handleConnect}
                    disabled={connecting}
                    className="inline-flex items-center gap-2 px-5 py-2.5 text-sm font-medium border-2 border-border rounded-lg hover:bg-bg-hover transition-colors disabled:opacity-50"
                  >
                    {connecting ? <Loader2 size={16} className="animate-spin" /> : <LogIn size={16} />}
                    {connecting ? t('setup.connecting') : t('setup.connectBtn')}
                  </button>

                  {connecting && (
                    <p className="text-xs text-text-muted max-w-[320px] leading-relaxed">
                      {t('setup.connectHint')}
                    </p>
                  )}

                  {connectError && !connecting && (
                    <div className="flex items-start gap-2 text-left p-3 bg-red-50 dark:bg-red-950/20 border border-red-200 dark:border-red-900 rounded-lg max-w-[340px]">
                      <AlertCircle size={14} className="text-red-600 dark:text-red-400 shrink-0 mt-0.5" />
                      <div>
                        <p className="text-[11px] text-red-700 dark:text-red-400">{connectError}</p>
                        {rateLimited && (
                          <p className="text-[11px] text-text-muted mt-1.5">{t('setup.connectRateLimited')}</p>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              )}

              <div className="flex justify-end">
                <button
                  onClick={() => setStep(4)}
                  disabled={xConnected !== true}
                  className="inline-flex items-center gap-2 px-5 py-2 bg-accent text-white font-medium text-sm rounded-lg hover:bg-accent-hover transition-colors disabled:opacity-50"
                >
                  {t('setup.continue')}
                  <ArrowRight size={14} />
                </button>
              </div>
            </div>
          )}

          {/* Step 4: Import Profile */}
          {step === 4 && (
            <div className="text-center">
              <h2 className="text-lg font-bold text-text mb-1">{t('setup.profileTitle')}</h2>
              <p className="text-sm text-text-muted mb-4">{t('setup.profileDesc')}</p>
              <p className="text-xs text-text-muted mb-6">{t('setup.profileWait')}</p>

              {profileResult ? (
                <div className="flex items-center gap-3 justify-center mb-6">
                  <img
                    src={api.profilePictureUrl()}
                    alt=""
                    className="w-11 h-11 rounded-full bg-bg-secondary object-cover border border-border"
                    onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
                  />
                  <div className="text-left">
                    <div className="flex items-center gap-1.5">
                      <span className="text-sm font-medium text-text">{profileResult.display_name}</span>
                      <CheckCircle2 size={14} className="text-green-600 dark:text-green-400" />
                    </div>
                    <span className="text-xs text-text-muted">@{profileResult.username}</span>
                  </div>
                </div>
              ) : (
                <div className="mb-6">
                  <button
                    onClick={handleFetchProfile}
                    disabled={fetchingProfile}
                    className="inline-flex items-center gap-2 px-5 py-2.5 text-sm font-medium border-2 border-border rounded-lg hover:bg-bg-hover transition-colors disabled:opacity-50"
                  >
                    {fetchingProfile ? <Loader2 size={16} className="animate-spin" /> : <User size={16} />}
                    {fetchingProfile ? t('setup.fetching') : t('setup.importProfile')}
                  </button>
                </div>
              )}

              <div className="flex justify-end">
                <button
                  onClick={handleFinish}
                  disabled={!profileResult}
                  className="inline-flex items-center gap-2 px-5 py-2 bg-accent text-white font-medium text-sm rounded-lg hover:bg-accent-hover transition-colors disabled:opacity-50"
                >
                  {t('setup.finish')}
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
