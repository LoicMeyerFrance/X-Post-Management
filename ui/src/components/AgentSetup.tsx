import { useState } from 'react'
import { Terminal, LogIn, Loader2, CheckCircle2, AlertCircle, RefreshCw, KeyRound, Ban } from 'lucide-react'
import { toast } from 'sonner'
import { useSettings } from '@/contexts/SettingsContext'
import { useNavigation } from '@/contexts/NavigationContext'
import * as api from '@/lib/api'

/** Getting Claude Code ready without asking the user to open a terminal.
 *
 *  Two things are needed - the binary and a signed-in account - and the CLI can
 *  do both itself: the official installer is one command, and `claude auth`
 *  reports the state and starts the browser sign-in. So this is two buttons, and
 *  each step shows whether it is already done rather than asking the user to
 *  guess. */
export function AgentSetup({
  status,
  onRefresh,
  onChosen,
}: {
  status: api.AgentStatus
  onRefresh: () => Promise<void> | void
  /** Called once a provider is picked, so the caller can close this panel. */
  onChosen?: () => void
}) {
  const { t } = useSettings()
  const { goTo } = useNavigation()
  const [installing, setInstalling] = useState(false)
  const [loggingIn, setLoggingIn] = useState(false)
  const [checking, setChecking] = useState(false)
  const [output, setOutput] = useState('')
  const [switching, setSwitching] = useState('')

  const chooseProvider = async (id: string) => {
    setSwitching(id)
    try {
      await api.setAgentProvider(id)
      await onRefresh()
      toast.success(t('agent.providerSwitched'))
      onChosen?.()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : t('common.serverError'))
    } finally {
      setSwitching('')
    }
  }

  const installed = status.cli_installed
  // A key makes the sign-in unnecessary: the run is billed to that API account.
  const authed = status.logged_in || status.has_api_key

  const install = async () => {
    setInstalling(true)
    setOutput('')
    try {
      const result = await api.installAgentCli()
      if (result.installed) {
        toast.success(t('agent.setupInstalled'))
      } else {
        toast.error(t('agent.setupInstallFailed'))
        setOutput(result.output)
      }
      await onRefresh()
    } catch {
      toast.error(t('common.serverError'))
    } finally {
      setInstalling(false)
    }
  }

  const login = async () => {
    setLoggingIn(true)
    try {
      const result = await api.loginAgentCli()
      if (result.started) toast.info(t('agent.setupLoginStarted'))
      else toast.error(result.detail || t('common.serverError'))
    } catch {
      toast.error(t('common.serverError'))
    } finally {
      setLoggingIn(false)
    }
  }

  const recheck = async () => {
    setChecking(true)
    try {
      await onRefresh()
    } finally {
      setChecking(false)
    }
  }

  const stepClasses = (done: boolean) =>
    `rounded-lg border p-4 transition-colors ${
      done ? 'border-success/40 bg-success-light/40' : 'border-border bg-bg-secondary'
    }`

  return (
    <div className="max-w-2xl space-y-3">
      {/* Which CLI drives it. Each meets the same bar, or it is not selectable. */}
      <h3 className="text-sm font-semibold text-text">{t('agent.providerPick')}</h3>
      <p className="text-xs leading-relaxed text-text-secondary">
        {t('agent.providerPickDesc')} {t('agent.providerDesc')}
      </p>
      {/* One card each, side by side: the choice is a comparison, and a
          stacked list made three short options look like a long form. */}
      <div className="grid gap-2.5 sm:grid-cols-3">
        {(status.providers || []).map(provider => {
          const chosen = provider.id === status.provider
          const busy = switching === provider.id
          return (
            <button
              key={provider.id}
              onClick={() => provider.available && chooseProvider(provider.id)}
              disabled={!provider.available || switching !== ''}
              title={provider.available ? provider.path || undefined : provider.unavailable_reason}
              className={`flex h-full flex-col items-start gap-1 rounded-lg border p-3 text-left transition-colors ${
                !provider.available
                  ? 'cursor-not-allowed border-border bg-bg-secondary/50 opacity-60'
                  : chosen
                    ? 'border-accent bg-accent/5 ring-1 ring-accent/30'
                    : 'border-border hover:border-accent/50 hover:bg-bg-hover'
              }`}
            >
              <div className="flex w-full items-center gap-1.5">
                {!provider.available
                  ? <Ban size={14} className="shrink-0 text-text-muted" />
                  : busy
                    ? <Loader2 size={14} className="shrink-0 animate-spin text-accent" />
                    : chosen
                      ? <CheckCircle2 size={14} className="shrink-0 text-accent" />
                      : <Terminal size={14} className="shrink-0 text-text-muted" />}
                <span className={`truncate text-[13px] font-semibold ${
                  chosen ? 'text-accent' : 'text-text'
                }`}>
                  {provider.label}
                </span>
              </div>

              <span className="text-[11px] text-text-muted">{provider.vendor}</span>

              <div className="flex flex-wrap items-center gap-1.5">
                {!provider.available ? (
                  <span className="rounded-full bg-bg-secondary px-2 py-0.5 text-[10px] text-text-muted">
                    {t('agent.providerBlocked')}
                  </span>
                ) : provider.installed ? (
                  <span className="rounded-full bg-success-light px-2 py-0.5 text-[10px] font-medium text-success">
                    {t('agent.providerInstalled')}
                  </span>
                ) : (
                  <span className="rounded-full bg-bg-secondary px-2 py-0.5 text-[10px] text-text-muted">
                    {t('agent.providerMissing')}
                  </span>
                )}
                {provider.version && (
                  <span className="font-mono text-[10px] text-text-muted">
                    v{provider.version}
                  </span>
                )}
              </div>

              {/* The reason a card is disabled, and the caveat on a weaker one,
                  belong on the card - not in a footnote the user scrolls past. */}
              {!provider.available && (
                <p className="text-[10px] leading-relaxed text-text-muted">
                  {provider.unavailable_reason}
                </p>
              )}
              {provider.available && provider.caveat && (
                <p className="flex items-start gap-1 text-[10px] leading-relaxed text-warning">
                  <AlertCircle size={10} className="mt-0.5 shrink-0" />
                  <span>{provider.caveat}</span>
                </p>
              )}
              {provider.available && !provider.installed && provider.install_command && (
                <code className="mt-auto block w-full overflow-hidden text-ellipsis whitespace-nowrap rounded border border-border bg-bg px-1.5 py-1 font-mono text-[9px] text-text-muted">
                  {provider.install_command}
                </code>
              )}
              {provider.available && provider.plan_note && (
                <p className="text-[10px] leading-relaxed text-text-muted">
                  {provider.plan_note}
                </p>
              )}
            </button>
          )
        })}
      </div>

      <h3 className="pt-1 text-sm font-semibold text-text">{t('agent.setupTitle')}</h3>
      <p className="text-xs leading-relaxed text-text-secondary">
        {t('agent.notInstalledBody')}
      </p>

      {/* Step 1: the binary */}
      <div className={stepClasses(installed)}>
        <div className="flex items-start gap-2">
          {installed
            ? <CheckCircle2 size={15} className="mt-0.5 shrink-0 text-success" />
            : <Terminal size={15} className="mt-0.5 shrink-0 text-text-muted" />}
          <div className="min-w-0 flex-1">
            <div className="text-[13px] font-medium text-text">
              1. {installed ? t('agent.setupInstalled') : t('agent.setupStep1')}
              {installed && status.cli_version && (
                <span className="ml-1.5 font-mono text-[11px] text-text-muted">
                  v{status.cli_version}
                </span>
              )}
            </div>
            {!installed && (
              <>
                <p className="mt-1 text-[11px] leading-relaxed text-text-secondary">
                  {t('agent.setupStep1Body')}
                </p>
                {/* Shown before it runs: this fetches and executes a remote
                    script, so the user sees exactly what they are agreeing to. */}
                <p className="mt-2 text-[10px] uppercase tracking-wide text-text-muted">
                  {t('agent.setupStep1Command')}
                </p>
                <pre className="mt-1 overflow-x-auto rounded border border-border bg-bg px-2 py-1.5 font-mono text-[11px] text-text">
                  {status.install_command}
                </pre>
                <button
                  onClick={install}
                  disabled={installing}
                  className="mt-3 inline-flex items-center gap-2 rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent-hover disabled:opacity-50"
                >
                  {installing ? <Loader2 size={13} className="animate-spin" /> : <Terminal size={13} />}
                  {installing ? t('agent.setupInstalling') : t('agent.setupInstall')}
                </button>
              </>
            )}
          </div>
        </div>
      </div>

      {/* Step 2: the account */}
      <div className={stepClasses(authed)}>
        <div className="flex items-start gap-2">
          {authed
            ? <CheckCircle2 size={15} className="mt-0.5 shrink-0 text-success" />
            : <LogIn size={15} className="mt-0.5 shrink-0 text-text-muted" />}
          <div className="min-w-0 flex-1">
            <div className="text-[13px] font-medium text-text">
              2. {authed ? t('agent.setupLoggedIn') : t('agent.setupStep2')}
              {status.plan && (
                <span className="ml-1.5 rounded-full bg-accent/10 px-2 py-0.5 text-[10px] font-medium uppercase text-accent">
                  {status.plan}
                </span>
              )}
            </div>
            {authed ? (
              <p className="mt-1 text-[11px] text-text-muted">
                {status.logged_in ? status.account_email : t('agent.usingApiKey')}
              </p>
            ) : (
              <>
                <p className="mt-1 text-[11px] leading-relaxed text-text-secondary">
                  {t('agent.setupStep2Body')}
                </p>
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <button
                    onClick={login}
                    disabled={loggingIn || !installed}
                    className="inline-flex items-center gap-2 rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent-hover disabled:opacity-40"
                  >
                    {loggingIn ? <Loader2 size={13} className="animate-spin" /> : <LogIn size={13} />}
                    {t('agent.setupLogin')}
                  </button>
                  <button
                    onClick={recheck}
                    disabled={checking}
                    className="inline-flex items-center gap-2 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-text-secondary transition-colors hover:bg-bg-hover disabled:opacity-50"
                  >
                    {checking ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
                    {t('agent.recheck')}
                  </button>
                  <button
                    onClick={() => goTo('settings', 'assistant')}
                    className="inline-flex items-center gap-1.5 text-[11px] text-text-muted underline-offset-2 hover:text-text-secondary hover:underline"
                  >
                    <KeyRound size={11} />
                    {t('agent.setupOrKey')}
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      </div>

      {/* Claude Code is not on the free plan, and finding that out on the first
          message would be a poor way to learn it. */}
      {!authed && (
        <p className="flex items-start gap-1.5 text-[11px] leading-relaxed text-text-muted">
          <AlertCircle size={12} className="mt-0.5 shrink-0" />
          {t('agent.setupPlanNote')}
        </p>
      )}

      {output && (
        <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded border border-error/30 bg-error-light/40 p-3 text-[10px] leading-relaxed text-text-secondary">
          {output}
        </pre>
      )}
    </div>
  )
}
