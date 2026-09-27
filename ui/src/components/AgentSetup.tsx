import { useState } from 'react'
import { Terminal, LogIn, Loader2, CheckCircle2, AlertCircle, RefreshCw, KeyRound } from 'lucide-react'
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
}: {
  status: api.AgentStatus
  onRefresh: () => Promise<void> | void
}) {
  const { t } = useSettings()
  const { goTo } = useNavigation()
  const [installing, setInstalling] = useState(false)
  const [loggingIn, setLoggingIn] = useState(false)
  const [checking, setChecking] = useState(false)
  const [output, setOutput] = useState('')

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
      <h3 className="text-sm font-semibold text-text">{t('agent.setupTitle')}</h3>
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
