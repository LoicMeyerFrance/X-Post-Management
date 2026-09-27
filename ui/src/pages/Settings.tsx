import { useState, useEffect, useCallback, useRef } from 'react'
import { Wifi, RefreshCw, Loader2, HelpCircle, X, Save, Pencil, AlertCircle, Trash2, BadgeCheck, Search, LogIn, Monitor, EyeOff, Bot, Terminal, KeyRound } from 'lucide-react'
import { toast } from 'sonner'
import { PageHeader } from '@/components/PageHeader'
import { useConfirm } from '@/components/ConfirmModal'
import { useSettings } from '@/contexts/SettingsContext'
import { useNavigation } from '@/contexts/NavigationContext'
import * as api from '@/lib/api'

export function Settings() {
  const { locale, setLocale, t, configured, recheckConfig, xConnected, checkingConnection, recheckConnection } = useSettings()
  const confirm = useConfirm()
  const [profile, setProfile] = useState<api.Profile | null>(null)
  const [testResult, setTestResult] = useState<{ success: boolean; error?: string; needs_manual_intervention?: boolean } | null>(null)
  const [testLoading, setTestLoading] = useState(false)
  const [fetchLoading, setFetchLoading] = useState(false)
  const [showEnvHelp, setShowEnvHelp] = useState(false)
  const [envValues, setEnvValues] = useState<Record<string, string>>({})
  const [envLoading, setEnvLoading] = useState(true)
  const [envSaving, setEnvSaving] = useState(false)
  const [envEditing, setEnvEditing] = useState(false)
  const [logoutLoading, setLogoutLoading] = useState(false)
  const [profileTs, setProfileTs] = useState(0)

  // Auto-edit when not configured
  useEffect(() => {
    if (configured === false) {
      setEnvEditing(true)
    }
  }, [configured])

  const loadEnv = useCallback(async () => {
    setEnvLoading(true)
    try {
      const data = await api.fetchEnvSettings()
      setEnvValues(data)
    } catch { /* ignore */ }
    finally { setEnvLoading(false) }
  }, [])

  useEffect(() => {
    api.fetchProfile().then(setProfile).catch(() => {})
    loadEnv()
  }, [loadEnv])

  const handleTestConnection = async () => {
    setTestLoading(true)
    setTestResult(null)
    try {
      const result = await api.testConnection()
      setTestResult(result)
      if (result.success) toast.success(t('settings.testSuccessToast'))
      else toast.error(t('settings.testFailed'))
    } catch {
      setTestResult({ success: false, error: t('settings.connectionServerError') })
      toast.error(t('common.serverError'))
    } finally {
      setTestLoading(false)
    }
  }

  const handleSaveEnv = async () => {
    setEnvSaving(true)
    try {
      const result = await api.saveEnvSettings(envValues)
      if (result.success) {
        toast.success(t('settings.configSaved'))
        loadEnv()
        recheckConfig()
        setEnvEditing(false)
      } else {
        toast.error(result.error || t('settings.errorUnknown'))
      }
    } catch {
      toast.error(t('common.serverError'))
    } finally {
      setEnvSaving(false)
    }
  }

  const handleLogout = async () => {
    if (!await confirm({ message: t('settings.logoutConfirm'), danger: true })) return
    setLogoutLoading(true)
    try {
      const result = await api.saveEnvSettings({ ...envValues, X_USERNAME: '', X_PASSWORD: '' })
      if (result.success) {
        toast.success(t('settings.logoutSuccess'))
        loadEnv()
        recheckConfig()
      } else {
        toast.error(result.error || t('settings.errorUnknown'))
      }
    } catch {
      toast.error(t('common.serverError'))
    } finally {
      setLogoutLoading(false)
    }
  }

  const handleFetchProfile = async () => {
    setFetchLoading(true)
    try {
      const result = await api.fetchProfileFromX()
      if (result.success) {
        toast.success(`${t('settings.profileFetched')} : ${result.display_name}`)
        const p = await api.fetchProfile()
        setProfile(p)
        setProfileTs(Date.now())
      } else {
        toast.error(`${t('settings.failPrefix')} : ${result.error || t('settings.errorUnknown')}`)
      }
    } catch {
      toast.error(t('common.serverError'))
    } finally {
      setFetchLoading(false)
    }
  }

  const [connectingX, setConnectingX] = useState(false)
  const [showAdvanced, setShowAdvanced] = useState(false)

  // Arriving from the dashboard hint: bring the browser switch into view and
  // flash it, so the setting being talked about is unmistakable.
  const { pendingSection, clearPendingSection } = useNavigation()
  const browserRef = useRef<HTMLDivElement>(null)
  const [highlightBrowser, setHighlightBrowser] = useState(false)

  useEffect(() => {
    if (pendingSection !== 'browser-mode') return
    clearPendingSection()
    browserRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    setHighlightBrowser(true)
    const timer = setTimeout(() => setHighlightBrowser(false), 2200)
    return () => clearTimeout(timer)
  }, [pendingSection, clearPendingSection])

  // Same treatment for the assistant section, reached from the assistant tab.
  const assistantRef = useRef<HTMLDivElement>(null)
  const [highlightAssistant, setHighlightAssistant] = useState(false)
  const [agentStatus, setAgentStatus] = useState<api.AgentStatus | null>(null)
  const [agentKey, setAgentKey] = useState('')
  const [agentKeySaving, setAgentKeySaving] = useState(false)

  useEffect(() => {
    if (pendingSection !== 'assistant') return
    clearPendingSection()
    assistantRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    setHighlightAssistant(true)
    const timer = setTimeout(() => setHighlightAssistant(false), 2200)
    return () => clearTimeout(timer)
  }, [pendingSection, clearPendingSection])

  const loadAgentStatus = useCallback(async () => {
    try {
      setAgentStatus(await api.fetchAgentStatus())
    } catch { /* the section degrades to its install hint */ }
  }, [])

  useEffect(() => { loadAgentStatus() }, [loadAgentStatus])

  const handleSaveAgentKey = async (value: string) => {
    setAgentKeySaving(true)
    try {
      await api.saveAgentKey(value)
      setAgentKey('')
      toast.success(value ? t('settings.assistantKeySaved') : t('settings.assistantKeyCleared'))
      await loadAgentStatus()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : t('common.serverError'))
    } finally {
      setAgentKeySaving(false)
    }
  }

  const handleConnectX = async () => {
    setConnectingX(true)
    toast.info(t('settings.connectXOpening'))
    try {
      const result = await api.connectX()
      if (result.success) {
        toast.success(t('settings.connectXSuccess'))
      } else {
        toast.error(result.error || t('settings.connectXFailed'))
      }
      await recheckConnection()
    } catch {
      toast.error(t('common.serverError'))
    } finally {
      setConnectingX(false)
    }
  }

  return (
    <div>
      <PageHeader title={t('settings.title')} description={t('settings.desc')} />

      {configured === false && (
        <div className="mx-6 mt-4 flex items-center gap-3 px-4 py-3 bg-blue-50 dark:bg-blue-950/30 border border-blue-200 dark:border-blue-800 rounded-lg">
          <AlertCircle size={18} className="text-blue-600 dark:text-blue-400 shrink-0" />
          <span className="text-sm font-medium text-blue-800 dark:text-blue-300">{t('settings.setupRequired')}</span>
        </div>
      )}

      {/* Profile */}
      <div className="px-6 py-6 border-b border-border">
        <h3 className="text-sm font-semibold text-text mb-3">{t('settings.profile')}</h3>
        <div className="flex items-center gap-3 mb-4">
          <img
            key={profileTs}
            src={api.profilePictureUrl() + '&t=' + (profileTs || Date.now())}
            alt=""
            className="w-10 h-10 rounded-full bg-bg-secondary object-cover border border-border"
            onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
          />
          <div>
            <div className="flex items-center gap-1.5">
              <span className="text-sm font-medium">{profile?.display_name || '-'}</span>
              {profile?.is_verified && (
                <BadgeCheck size={16} className={
                  profile.verified_type === 'business' ? 'text-[#E0A526]' :
                  profile.verified_type === 'government' ? 'text-[#829AAB]' :
                  'text-[#1D9BF0]'
                } />
              )}
            </div>
            <div className="text-xs text-text-muted">{profile?.username ? `@${profile.username}` : '-'}</div>
          </div>
        </div>
        <button
          onClick={handleFetchProfile}
          disabled={fetchLoading}
          className="inline-flex items-center gap-2 px-3 py-1.5 text-xs font-medium text-text-secondary border border-border rounded-md hover:bg-bg-hover transition-colors disabled:opacity-50"
        >
          {fetchLoading ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
          {t('settings.fetchProfile')}
        </button>
      </div>

      {/* Config */}
      <div className="px-6 py-6 border-b border-border">
        <div className="flex items-center gap-2 mb-4">
          <h3 className="text-sm font-semibold text-text">{t('settings.config')}</h3>
          <button
            onClick={() => setShowEnvHelp(!showEnvHelp)}
            className="w-5 h-5 flex items-center justify-center rounded-full text-text-muted hover:text-text hover:bg-bg-hover transition-colors"
            title={t('settings.configHelp')}
          >
            <HelpCircle size={15} />
          </button>
        </div>

        {showEnvHelp && (
          <div className="mb-4 p-4 bg-bg-secondary border border-border rounded-lg relative">
            <button onClick={() => setShowEnvHelp(false)} className="absolute top-2 right-2 text-text-muted hover:text-text">
              <X size={14} />
            </button>
            <h4 className="text-xs font-semibold text-text mb-2">{t('settings.configHelpTitle')}</h4>
            <ul className="text-xs text-text-muted space-y-1">
              <li><strong>{t('settings.labelUsername')}</strong> : {t('settings.envUsername')}</li>
              <li><strong>{t('settings.labelPassword')}</strong> : {t('settings.envPassword')}</li>
              <li><strong>{t('settings.labelChromeProfile')}</strong> : {t('settings.envChrome')}</li>
              <li><strong>{t('settings.labelChromePath')}</strong> : {t('settings.envChromePath')}</li>
              <li><strong>{t('settings.labelBrowser')}</strong> : {t('settings.envHeadlessToggle')}</li>
              <li><strong>{t('settings.labelCheckInterval')}</strong> : {t('settings.envInterval')}</li>
              <li><strong>{t('settings.labelMaxRetries')}</strong> : {t('settings.envMaxRetries')}</li>
              <li><strong>{t('settings.connectX')}</strong> : {t('settings.connectXHelp')}</li>
            </ul>

            <h5 className="text-xs font-semibold text-text mt-3 mb-2">{t('settings.chromePathsTitle')}</h5>
            <div className="space-y-2">
              <div>
                <span className="text-[11px] font-semibold text-text-secondary">Windows</span>
                <pre className="text-[10px] font-mono bg-bg p-2 rounded mt-0.5 text-text-muted overflow-x-auto">{`C:\\Users\\<user>\\AppData\\Local\\Google\\Chrome\\User Data\\Default
C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe`}</pre>
              </div>
            </div>
          </div>
        )}

        {envLoading ? (
          <div className="text-sm text-text-muted py-4">{t('common.loading')}</div>
        ) : envEditing ? (
          <div className="space-y-3">
            {/* X_USERNAME with @ strip and email warning */}
            <div>
              <div className="flex items-center gap-3">
                <label className="w-52 shrink-0 text-xs font-medium text-text">{t('settings.labelUsername')}</label>
                <input
                  type="text"
                  value={envValues['X_USERNAME'] || ''}
                  onChange={e => {
                    let val = e.target.value
                    if (val.startsWith('@')) val = val.slice(1)
                    setEnvValues(prev => ({ ...prev, X_USERNAME: val }))
                  }}
                  className="flex-1 px-2.5 py-1.5 text-xs border border-border rounded-md focus:outline-none focus:border-accent bg-bg-secondary/50 text-text"
                />
              </div>
              {envValues['X_USERNAME']?.includes('@') && (
                <div className="ml-[13.5rem] mt-1 text-[11px] text-amber-600 dark:text-amber-400">
                  {t('settings.usernameNotEmail')}
                </div>
              )}
            </div>
            {[
              { key: 'X_PASSWORD', label: t('settings.labelPassword'), type: 'password', detect: false },
              { key: 'CHECK_INTERVAL_SECONDS', label: t('settings.labelCheckInterval'), type: 'text', detect: false },
              { key: 'MAX_RETRIES', label: t('settings.labelMaxRetries'), type: 'text', detect: false },
              ...(showAdvanced ? [
                { key: 'CHROME_PROFILE_DIR', label: t('settings.labelChromeProfile'), type: 'text', detect: true },
                { key: 'CHROME_PATH', label: t('settings.labelChromePath'), type: 'text', detect: true },
              ] : []),
            ].map(field => (
              <div key={field.key} className="flex items-center gap-3">
                <label className="w-52 shrink-0 text-xs font-medium text-text">{field.label}</label>
                <input
                  type={field.type}
                  value={envValues[field.key] || ''}
                  onChange={e => setEnvValues(prev => ({ ...prev, [field.key]: e.target.value }))}
                  className="flex-1 px-2.5 py-1.5 text-xs border border-border rounded-md focus:outline-none focus:border-accent bg-bg-secondary/50 text-text"
                  placeholder={field.key === 'X_PASSWORD' ? '••••••••' : ''}
                />
                {field.detect && (
                  <button
                    type="button"
                    onClick={async () => {
                      const result = await api.detectChrome()
                      if (result.detected) {
                        setEnvValues(prev => ({
                          ...prev,
                          ...(result.profile_dir ? { CHROME_PROFILE_DIR: result.profile_dir } : {}),
                          ...(result.chrome_path ? { CHROME_PATH: result.chrome_path } : {}),
                        }))
                        toast.success(t('settings.chromeDetected'))
                      } else {
                        toast.error(t('settings.chromeNotFound'))
                      }
                    }}
                    className="p-1.5 text-text-muted hover:text-text hover:bg-bg-hover rounded-md transition-colors"
                    title={t('settings.detectChrome')}
                  >
                    <Search size={16} />
                  </button>
                )}
              </div>
            ))}
            {/* Advanced: forcing a specific Chrome is almost never needed */}
            <div className="flex items-center gap-3">
              <span className="w-52 shrink-0" />
              <button
                type="button"
                onClick={() => setShowAdvanced(v => !v)}
                className="text-[11px] text-text-muted hover:text-text underline underline-offset-2"
              >
                {showAdvanced ? '- ' : '+ '}{t('settings.advanced')}
              </button>
              {!showAdvanced && (
                <span className="text-[11px] text-text-muted/70">{t('settings.advancedHint')}</span>
              )}
            </div>

            {/* Connect to X */}
            <div className="flex items-center gap-3">
              <label className="w-52 shrink-0 text-xs font-medium text-text">{t('settings.connectX')}</label>
              <div className="flex-1">
                <button
                  type="button"
                  onClick={handleConnectX}
                  disabled={checkingConnection || connectingX}
                  className={`inline-flex items-center gap-2 px-3 py-1.5 text-xs font-medium rounded-md transition-colors disabled:opacity-50 ${
                    xConnected === true
                      ? 'text-green-700 dark:text-green-400 border-2 border-green-500 hover:bg-green-50 dark:hover:bg-green-950/30'
                      : xConnected === false
                        ? 'text-red-700 dark:text-red-400 border-2 border-red-500 hover:bg-red-50 dark:hover:bg-red-950/30'
                        : 'text-text-secondary border border-border hover:bg-bg-hover'
                  }`}
                >
                  {checkingConnection || connectingX ? <Loader2 size={13} className="animate-spin" /> : <LogIn size={13} />}
                  {t('settings.connectX')}
                </button>
                <p className="text-[10px] text-text-muted mt-1">{t('settings.connectXDesc')}</p>
              </div>
            </div>
            <div className="flex items-center gap-2 pt-2">
              <button
                onClick={handleSaveEnv}
                disabled={envSaving}
                className="inline-flex items-center gap-2 px-3 py-1.5 text-xs font-medium text-white bg-accent rounded-md hover:bg-accent-hover transition-colors disabled:opacity-50"
              >
                {envSaving ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />}
                {t('common.save')}
              </button>
              <button
                onClick={() => { setEnvEditing(false); loadEnv() }}
                className="px-3 py-1.5 text-xs text-text-muted hover:text-text rounded-md hover:bg-bg-hover transition-colors"
              >
                {t('common.cancel')}
              </button>
            </div>
          </div>
        ) : (
          <>
            <table className="w-full text-sm">
              <tbody>
                {[
                  { label: t('settings.labelUsername'), value: '***' },
                  { label: t('settings.labelPassword'), value: '********' },
                  { label: t('settings.labelCheckInterval'), value: envValues['CHECK_INTERVAL_SECONDS'] ? envValues['CHECK_INTERVAL_SECONDS'] + 's' : '15s' },
                  { label: t('settings.labelMaxRetries'), value: envValues['MAX_RETRIES'] || '1' },
                  { label: t('settings.labelChromeProfile'), value: envValues['CHROME_PROFILE_DIR'] || t('settings.automatic') },
                  { label: t('settings.labelChromePath'), value: envValues['CHROME_PATH'] || t('settings.automatic') },
                ].map(row => (
                  <tr key={row.label} className="border-b border-border last:border-0">
                    <td className="py-2.5 pr-4 text-xs font-medium text-text w-52">{row.label}</td>
                    <td className="py-2.5 text-xs text-text-muted">{row.value}</td>
                  </tr>
                ))}
                <tr className="border-b border-border last:border-0">
                  <td className="py-2.5 pr-4 text-xs font-medium text-text w-52">{t('settings.connectX')}</td>
                  <td className="py-2.5">
                    <button
                      onClick={handleConnectX}
                      disabled={checkingConnection || connectingX}
                      className={`inline-flex items-center gap-2 px-2.5 py-1 text-xs font-medium rounded-md transition-colors disabled:opacity-50 ${
                        xConnected === true
                          ? 'text-green-700 dark:text-green-400 border-2 border-green-500 hover:bg-green-50 dark:hover:bg-green-950/30'
                          : xConnected === false
                            ? 'text-red-700 dark:text-red-400 border-2 border-red-500 hover:bg-red-50 dark:hover:bg-red-950/30'
                            : 'text-text-secondary border border-border hover:bg-bg-hover'
                      }`}
                    >
                      {checkingConnection || connectingX ? <Loader2 size={12} className="animate-spin" /> : <LogIn size={12} />}
                      {t('settings.connectX')}
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
            <button
              onClick={() => setEnvEditing(true)}
              className="mt-3 inline-flex items-center gap-2 px-3 py-1.5 text-xs font-medium text-accent border border-accent/30 bg-accent/5 rounded-md hover:bg-accent/10 transition-colors"
            >
              <Pencil size={13} />
              {t('settings.editConfig')}
            </button>
          </>
        )}
      </div>

      {/* Browser */}
      <div
        id="browser-mode"
        ref={browserRef}
        className={`px-6 py-6 border-b border-border transition-colors duration-500 ${
          highlightBrowser ? 'bg-accent-light dark:bg-accent/15' : ''
        }`}
      >
        <h3 className="text-sm font-semibold text-text mb-4">{t('settings.browserSection')}</h3>
        <div className="grid grid-cols-2 gap-3">
          {([
            { value: 'true', icon: EyeOff, titleKey: 'settings.browserInvisibleTitle' as const, descKey: 'settings.browserInvisibleDesc' as const },
            { value: 'false', icon: Monitor, titleKey: 'settings.browserVisibleTitle' as const, descKey: 'settings.browserVisibleDesc' as const },
          ] as const).map(opt => {
            const active = (envValues.HEADLESS === 'true') === (opt.value === 'true')
            const Icon = opt.icon
            return (
              <button
                key={opt.value}
                type="button"
                onClick={async () => {
                  if (active) return
                  const updated = { ...envValues, HEADLESS: opt.value }
                  setEnvValues(updated)
                  try {
                    const result = await api.saveEnvSettings(updated)
                    if (result.success) {
                      toast.success(t('settings.configSaved'))
                    } else {
                      toast.error(result.error || t('settings.errorUnknown'))
                    }
                  } catch {
                    toast.error(t('common.serverError'))
                  }
                }}
                className={`flex items-start gap-3 p-4 rounded-lg border text-left transition-colors cursor-pointer ${active ? 'border-accent bg-accent/5' : 'border-border hover:border-text-muted hover:bg-bg-hover'}`}
              >
                <Icon size={24} className={`shrink-0 mt-0.5 ${active ? 'text-accent' : 'text-text-muted'}`} />
                <div>
                  <div className={`text-xs font-semibold ${active ? 'text-accent' : 'text-text'}`}>{t(opt.titleKey)}</div>
                  <div className="text-[11px] text-text-muted mt-0.5">{t(opt.descKey)}</div>
                </div>
              </button>
            )
          })}
        </div>
      </div>

      {/* Assistant */}
      <div
        id="assistant"
        ref={assistantRef}
        className={`px-6 py-6 border-b border-border transition-colors duration-500 ${
          highlightAssistant ? 'bg-accent-light dark:bg-accent/15' : ''
        }`}
      >
        <h3 className="text-sm font-semibold text-text mb-2 flex items-center gap-2">
          <Bot size={15} className="text-text-secondary" />
          {t('settings.assistantSection')}
        </h3>
        <p className="text-xs text-text-muted mb-4 max-w-2xl leading-relaxed">
          {t('settings.assistantIntro')}
        </p>

        <div className="flex items-center gap-2 text-xs mb-4">
          <Terminal size={13} className={agentStatus?.cli_installed ? 'text-success' : 'text-warning'} />
          {agentStatus?.cli_installed ? (
            <span className="text-success">
              {t('settings.assistantCliFound')}
              {agentStatus.cli_version && <span className="font-mono ml-1.5">v{agentStatus.cli_version}</span>}
            </span>
          ) : (
            <span className="text-warning">{t('settings.assistantCliMissing')}</span>
          )}
        </div>

        <label className="block text-xs font-medium text-text-secondary mb-1">
          {t('settings.assistantKeyLabel')}
        </label>
        <p className="text-[11px] text-text-muted mb-2 max-w-2xl leading-relaxed">
          {t('settings.assistantKeyDesc')}
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative flex-1 min-w-[240px]">
            <KeyRound size={13} className="absolute left-3 top-1/2 -translate-y-1/2 text-text-muted" />
            <input
              type="password"
              value={agentKey}
              onChange={e => setAgentKey(e.target.value)}
              placeholder={agentStatus?.has_api_key ? '••••••••••••••••' : 'sk-ant-...'}
              autoComplete="off"
              spellCheck={false}
              className="w-full pl-8 pr-3 py-2 text-sm border border-border rounded-lg bg-bg text-text placeholder:text-text-muted focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent/20"
            />
          </div>
          <button
            onClick={() => handleSaveAgentKey(agentKey)}
            disabled={agentKeySaving || !agentKey.trim()}
            className="inline-flex items-center gap-2 px-3 py-2 text-xs font-medium text-white bg-accent rounded-md hover:bg-accent-hover transition-colors disabled:opacity-40"
          >
            {agentKeySaving ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />}
            {t('common.save')}
          </button>
          {agentStatus?.has_api_key && (
            <button
              onClick={() => handleSaveAgentKey('')}
              disabled={agentKeySaving}
              className="inline-flex items-center gap-2 px-3 py-2 text-xs font-medium text-text-secondary border border-border rounded-md hover:bg-bg-hover transition-colors disabled:opacity-50"
            >
              <Trash2 size={13} />
              {t('settings.assistantKeyClear')}
            </button>
          )}
        </div>
      </div>

      {/* Language */}
      <div className="px-6 py-6 border-b border-border">
        <h3 className="text-sm font-semibold text-text mb-4">{t('settings.language')}</h3>
        <select
          value={locale}
          onChange={e => setLocale(e.target.value as 'fr' | 'en')}
          className="px-3 py-2 text-sm border border-border rounded-lg focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent/20 bg-bg text-text [&>option]:bg-bg [&>option]:text-text"
        >
          <option value="en">English</option>
          <option value="fr">Français</option>
        </select>
      </div>

      {/* Test connection */}
      <div className="px-6 py-6 border-b border-border">
        <h3 className="text-sm font-semibold text-text mb-2">{t('settings.testConnection')}</h3>
        <p className="text-xs text-text-muted mb-3">{t('settings.testConnectionDesc')}</p>
        <button
          onClick={handleTestConnection}
          disabled={testLoading}
          className="inline-flex items-center gap-2 px-3 py-1.5 text-xs font-medium text-text-secondary border border-border rounded-md hover:bg-bg-hover transition-colors disabled:opacity-50"
        >
          {testLoading ? <Loader2 size={13} className="animate-spin" /> : <Wifi size={13} />}
          {t('settings.testBtn')}
        </button>

        {testResult && (
          <div className={`mt-3 px-3 py-2.5 rounded-md text-xs ${testResult.success ? 'bg-success-light text-success' : 'bg-error-light text-error'}`}>
            {testResult.success
              ? t('settings.testSuccess')
              : `${t('settings.failPrefix')} : ${testResult.error || t('settings.errorUnknown')}${testResult.needs_manual_intervention ? '\n' + t('settings.manualIntervention') : ''}`
            }
          </div>
        )}
      </div>

      {/* Logout */}
      {configured && (
        <div className="px-6 py-6">
          <h3 className="text-sm font-semibold text-red-600 dark:text-red-400 mb-1">{t('settings.logout')}</h3>
          <p className="text-xs text-text-muted mb-3">{t('settings.logoutDesc')}</p>
          <button
            onClick={handleLogout}
            disabled={logoutLoading}
            className="inline-flex items-center gap-2 px-3 py-1.5 text-xs font-medium text-white bg-red-600 rounded-md hover:bg-red-700 transition-colors disabled:opacity-50"
          >
            {logoutLoading ? <Loader2 size={13} className="animate-spin" /> : <Trash2 size={13} />}
            {t('settings.logout')}
          </button>
        </div>
      )}
    </div>
  )
}
