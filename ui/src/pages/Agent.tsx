import { useEffect, useRef } from 'react'
import {
  Bot, Send, Square, RotateCcw, Loader2, Wrench, AlertCircle,
  ShieldCheck, Zap, CheckCircle2,
  Check, X, CalendarClock, Image as ImageIcon, Trash2, Globe, GlobeLock, Copy,
} from 'lucide-react'
import { toast } from 'sonner'
import { PageHeader } from '@/components/PageHeader'
import { useSettings } from '@/contexts/SettingsContext'
import { useNavigation } from '@/contexts/NavigationContext'
import { useAgent } from '@/contexts/AgentContext'
import { AgentSetup } from '@/components/AgentSetup'
import { copyText } from '@/lib/clipboard'

/** Display only. The transcript and the running turn live in AgentContext, so
 *  switching tabs mid-answer neither loses the conversation nor stops it. */
export function Agent() {
  const { t } = useSettings()
  const { goTo } = useNavigation()
  const {
    status, entries, live, busy, draft, setDraft,
    send, stop, newConversation, setAuto, setWeb, refreshStatus, actOnProposal,
  } = useAgent()

  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [entries, live])

  // Coming back to the tab, pick up anything that changed while away.
  useEffect(() => { refreshStatus() }, [refreshStatus])

  const changeAuto = async (enabled: boolean) => {
    if (!(await setAuto(enabled))) toast.error(t('common.serverError'))
  }

  const changeWeb = async (enabled: boolean) => {
    if (!(await setWeb(enabled))) toast.error(t('common.serverError'))
  }

  const copy = async (text: string) => {
    if (await copyText(text)) toast.success(t('common.copied'))
    else toast.error(t('common.copyFailed'))
  }

  const onKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      send(draft)
    }
  }

  // --- Claude Code missing: nothing else on the page can work ---
  // Not ready covers both a missing binary and a CLI that is not signed in.
  // Showing the chat in either case sent the user's first message into a failure.
  if (status && !status.ready) {
    return (
      <div>
        <PageHeader title={t('agent.title')} description={t('agent.desc')} />
        <div className="px-6 py-6">
          <AgentSetup status={status} onRefresh={refreshStatus} />
        </div>
      </div>
    )
  }

  const auto = !!status?.auto_approve
  const web = !!status?.web_access

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title={t('agent.title')}
        description={t('agent.desc')}
        actions={
          <button
            onClick={newConversation}
            className="inline-flex items-center gap-2 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-text-secondary transition-colors hover:bg-bg-hover"
            title={t('agent.newConversation')}
          >
            <RotateCcw size={13} />
            {t('agent.newConversation')}
          </button>
        }
      />

      {/* Approval mode. Manual is the default and stays one click away. */}
      <div className="border-b border-border px-6 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-xs font-medium text-text-secondary">{t('agent.approvalLabel')}</span>
          <div className="inline-flex overflow-hidden rounded-md border border-border">
            <button
              onClick={() => changeAuto(false)}
              className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium transition-colors ${
                !auto ? 'bg-accent text-white' : 'text-text-secondary hover:bg-bg-hover'
              }`}
            >
              <ShieldCheck size={13} />
              {t('agent.manualMode')}
            </button>
            <button
              onClick={() => changeAuto(true)}
              className={`inline-flex items-center gap-1.5 border-l border-border px-3 py-1.5 text-xs font-medium transition-colors ${
                auto ? 'bg-warning text-white' : 'text-text-secondary hover:bg-bg-hover'
              }`}
            >
              <Zap size={13} />
              {t('agent.autoMode')}
            </button>
          </div>
          <span className="text-xs font-medium text-text-secondary">{t('agent.webLabel')}</span>
          <div className="inline-flex overflow-hidden rounded-md border border-border">
            <button
              onClick={() => changeWeb(false)}
              className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium transition-colors ${
                !web ? 'bg-text-secondary text-white' : 'text-text-secondary hover:bg-bg-hover'
              }`}
            >
              <GlobeLock size={13} />
              {t('agent.webOff')}
            </button>
            <button
              onClick={() => changeWeb(true)}
              className={`inline-flex items-center gap-1.5 border-l border-border px-3 py-1.5 text-xs font-medium transition-colors ${
                web ? 'bg-accent text-white' : 'text-text-secondary hover:bg-bg-hover'
              }`}
            >
              <Globe size={13} />
              {t('agent.webOn')}
            </button>
          </div>
          {status && (
            <span className="ml-auto inline-flex items-center gap-1.5 text-[11px] text-text-muted">
              <CheckCircle2 size={12} className="text-success" />
              {status.auth_mode === 'api_key' ? t('agent.authApiKey') : t('agent.authSubscription')}
              {status.cli_version && <span className="font-mono">v{status.cli_version}</span>}
            </span>
          )}
        </div>
        <p className={`mt-2 text-[11px] leading-relaxed ${auto ? 'text-warning' : 'text-text-muted'}`}>
          {auto ? t('agent.autoModeDesc') : t('agent.manualModeDesc')}
        </p>
        <p className="mt-1 text-[11px] leading-relaxed text-text-muted">
          {web ? t('agent.webOnDesc') : t('agent.webOffDesc')}
        </p>
      </div>

      {/* Transcript */}
      <div className="flex-1 overflow-y-auto px-6 py-5">
        {entries.length === 0 && !live && (
          <div className="mx-auto max-w-lg py-10 text-center">
            <Bot size={30} className="mx-auto text-text-muted" />
            <h3 className="mt-3 text-sm font-semibold text-text">{t('agent.emptyTitle')}</h3>
            <p className="mt-1 text-xs text-text-muted">{t('agent.emptyBody')}</p>
            <div className="mt-5 space-y-2">
              {[t('agent.example1'), t('agent.example2'), t('agent.example3')].map(example => (
                <button
                  key={example}
                  onClick={() => send(example)}
                  className="block w-full rounded-lg border border-border px-3 py-2 text-left text-xs text-text-secondary transition-colors hover:border-accent hover:bg-accent/5 hover:text-text"
                >
                  {example}
                </button>
              ))}
            </div>
          </div>
        )}

        <div className="mx-auto max-w-3xl space-y-3">
          {entries.map((entry, index) => {
            if (entry.kind === 'user') {
              return (
                <div key={index} className="flex justify-end">
                  <div className="max-w-[80%] whitespace-pre-wrap rounded-2xl rounded-br-sm bg-accent px-4 py-2.5 text-[13px] text-white">
                    {entry.text}
                  </div>
                </div>
              )
            }
            if (entry.kind === 'assistant') {
              return (
                <div key={index} className="group flex items-start justify-start gap-1.5">
                  <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-bl-sm border border-border bg-bg-secondary px-4 py-2.5 text-[13px] leading-relaxed text-text">
                    {entry.text}
                  </div>
                  <button
                    onClick={() => copy(entry.text)}
                    title={t('common.copy')}
                    className="mt-1.5 shrink-0 rounded-md p-1.5 text-text-muted opacity-0 transition-opacity hover:bg-bg-hover hover:text-text group-hover:opacity-100 focus:opacity-100"
                  >
                    <Copy size={12} />
                  </button>
                </div>
              )
            }
            if (entry.kind === 'tool') {
              return (
                <div key={index} className="flex items-start gap-2 pl-1 text-[11px] text-text-muted">
                  <Wrench size={12} className="mt-0.5 shrink-0" />
                  <span>
                    <span className="font-medium text-text-secondary">{entry.name}</span>
                    {entry.summary && <span className="ml-1.5">{entry.summary}</span>}
                  </span>
                </div>
              )
            }
            if (entry.kind === 'proposal') {
              const p = entry.proposal
              // What the tick means depends on the post: a dated one goes to X's
              // own scheduler, an undated one goes out now. The button says which.
              const approve = p.scheduledAt ? 'scheduleOnX' as const : 'publish' as const
              const approveLabel = p.scheduledAt
                ? t('agent.proposalSchedule')
                : t('agent.proposalPublish')
              const settled: Record<string, { label: string; tone: string }> = {
                published: { label: t('agent.proposalPublished'), tone: 'text-success' },
                scheduled: { label: t('agent.proposalScheduled'), tone: 'text-success' },
                deleted: { label: t('agent.proposalDeleted'), tone: 'text-text-muted' },
              }
              return (
                <div key={index} className="flex justify-start">
                  <div className={`w-full max-w-[85%] rounded-xl border p-3 ${
                    p.auto ? 'border-warning/40 bg-warning/5' : 'border-accent/40 bg-accent/5'
                  }`}>
                    <div className={`mb-2 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide ${
                      p.auto ? 'text-warning' : 'text-accent'
                    }`}>
                      {p.auto ? <Zap size={11} /> : <ShieldCheck size={11} />}
                      {p.auto ? t('agent.proposalTitleAuto') : t('agent.proposalTitle')}
                      <span className="font-mono font-normal normal-case tracking-normal text-text-muted">
                        #{p.postId}
                      </span>
                    </div>

                    <div className="flex items-start gap-1.5">
                      <p className="min-w-0 flex-1 whitespace-pre-wrap text-[13px] leading-relaxed text-text">
                        {p.text || <span className="italic text-text-muted">{t('agent.proposalNoText')}</span>}
                      </p>
                      {p.text && (
                        <button
                          onClick={() => copy(p.text)}
                          title={t('common.copy')}
                          className="shrink-0 rounded-md p-1.5 text-text-muted transition-colors hover:bg-bg-hover hover:text-text"
                        >
                          <Copy size={12} />
                        </button>
                      )}
                    </div>

                    {(p.scheduledAt || p.media) && (
                      <div className="mt-2 flex flex-wrap items-center gap-3 text-[11px] text-text-muted">
                        {p.scheduledAt && (
                          <span className="inline-flex items-center gap-1">
                            <CalendarClock size={11} />
                            {p.scheduledAt.replace('T', ' ')}
                          </span>
                        )}
                        {p.media && (
                          <span className="inline-flex items-center gap-1">
                            <ImageIcon size={11} />
                            {p.media}
                          </span>
                        )}
                      </div>
                    )}

                    <div className="mt-3 flex items-center gap-2">
                      {p.state === 'pending' && (
                        <>
                          <button
                            onClick={() => actOnProposal(index, approve)}
                            className="inline-flex items-center gap-1.5 rounded-md bg-success px-3 py-1.5 text-[11px] font-medium text-white transition-opacity hover:opacity-90"
                          >
                            <Check size={12} strokeWidth={3} />
                            {approveLabel}
                          </button>
                          <button
                            onClick={() => actOnProposal(index, 'discard')}
                            className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-[11px] font-medium text-text-secondary transition-colors hover:bg-bg-hover hover:text-error"
                          >
                            <X size={12} strokeWidth={3} />
                            {t('agent.proposalDiscard')}
                          </button>
                          <span className="ml-auto text-[10px] text-text-muted">
                            {t('agent.proposalHint')}
                          </span>
                        </>
                      )}
                      {p.state === 'working' && (
                        <span className="inline-flex items-center gap-1.5 text-[11px] text-text-muted">
                          <Loader2 size={12} className="animate-spin" />
                          {t('agent.proposalWorking')}
                        </span>
                      )}
                      {settled[p.state] && (
                        <span className={`inline-flex items-center gap-1.5 text-[11px] font-medium ${settled[p.state].tone}`}>
                          {p.state === 'deleted'
                            ? <Trash2 size={12} />
                            : <CheckCircle2 size={12} />}
                          {settled[p.state].label}
                        </span>
                      )}
                      {p.state === 'error' && (
                        <span className="inline-flex items-start gap-1.5 text-[11px] text-error">
                          <AlertCircle size={12} className="mt-0.5 shrink-0" />
                          {p.detail || t('common.unknownError')}
                        </span>
                      )}
                    </div>
                  </div>
                </div>
              )
            }
            // A note: a code is translated now, so switching language updates it.
            const label = entry.code === 'mcpFailed' ? t('agent.mcpFailed')
              : entry.code === 'stopped' ? t('agent.stopped')
              : ''
            const body = [label, entry.text].filter(Boolean).join(entry.code ? ' — ' : '')
            return (
              <div
                key={index}
                className={`flex items-start gap-2 rounded-md px-3 py-2 text-[11px] ${
                  entry.tone === 'error' ? 'bg-error-light text-error' : 'bg-bg-secondary text-text-muted'
                }`}
              >
                {entry.tone === 'error' && <AlertCircle size={12} className="mt-0.5 shrink-0" />}
                <span className="whitespace-pre-wrap">{body || t('common.unknownError')}</span>
              </div>
            )
          })}

          {live && (
            <div className="flex justify-start">
              <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-bl-sm border border-border bg-bg-secondary px-4 py-2.5 text-[13px] leading-relaxed text-text">
                {live}
              </div>
            </div>
          )}

          {busy && !live && (
            <div className="flex items-center gap-2 pl-1 text-[11px] text-text-muted">
              <Loader2 size={12} className="animate-spin" />
              {t('agent.working')}
            </div>
          )}
        </div>
        <div ref={bottomRef} />
      </div>

      {/* Composer */}
      <div className="border-t border-border bg-bg px-6 py-4">
        {auto && (
          <p className="mx-auto mb-2 flex max-w-3xl items-center gap-1.5 text-[11px] text-warning">
            <Zap size={12} />
            {t('agent.autoOnWarning')}
          </p>
        )}
        <div className="mx-auto flex max-w-3xl items-end gap-2">
          <textarea
            value={draft}
            onChange={e => setDraft(e.target.value)}
            onKeyDown={onKeyDown}
            rows={2}
            placeholder={t('agent.placeholder')}
            className="flex-1 resize-none rounded-lg border border-border bg-bg px-3 py-2.5 text-[13px] text-text placeholder:text-text-muted focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent/20"
          />
          {busy ? (
            <button
              onClick={stop}
              className="inline-flex items-center gap-2 rounded-lg bg-error px-4 py-2.5 text-xs font-medium text-white transition-colors hover:opacity-90"
            >
              <Square size={13} />
              {t('agent.stop')}
            </button>
          ) : (
            <button
              onClick={() => send(draft)}
              disabled={!draft.trim()}
              className="inline-flex items-center gap-2 rounded-lg bg-accent px-4 py-2.5 text-xs font-medium text-white transition-colors hover:bg-accent-hover disabled:opacity-40"
            >
              <Send size={13} />
              {t('agent.send')}
            </button>
          )}
        </div>
        <button
          onClick={() => goTo('settings', 'assistant')}
          className="mx-auto mt-2 block max-w-3xl text-[11px] text-text-muted underline-offset-2 hover:text-text-secondary hover:underline"
        >
          {t('agent.openSettings')}
        </button>
      </div>
    </div>
  )
}
