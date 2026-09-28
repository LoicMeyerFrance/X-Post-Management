import {
  createContext, useContext, useState, useCallback, useRef, useEffect, type ReactNode,
} from 'react'
import * as api from '@/lib/api'
import { toolLabel, summariseInput, extractPostId } from '@/lib/agentFormat'

/** One line in the transcript.
 *
 *  Notes carry a `code` rather than a translated string: the provider has no
 *  business knowing the locale, and a message stored as a code is re-translated
 *  when the user switches language instead of being frozen in the old one. */
export type Entry =
  | { kind: 'user'; text: string }
  | { kind: 'assistant'; text: string }
  | { kind: 'tool'; name: string; summary: string }
  | { kind: 'note'; tone: 'info' | 'error'; code?: 'mcpFailed' | 'stopped'; text?: string }
  | { kind: 'proposal'; proposal: Proposal }

/** A post the agent created, waiting for the user's yes or no.
 *
 *  This is the approval that matters: not "may the agent call a tool", but "does
 *  this post go out". Acting on it uses the same endpoints as the calendar, so a
 *  yes here is exactly a yes there. */
export interface Proposal {
  postId: number
  text: string
  scheduledAt: string | null
  media: string | null
  state: 'pending' | 'working' | 'published' | 'scheduled' | 'deleted' | 'error'
  detail?: string
  /** True when automatic mode pressed the tick instead of the user. */
  auto?: boolean
}

export type ProposalAction = 'publish' | 'scheduleOnX' | 'discard'

/** A long session should not grow the DOM without bound. */
const MAX_ENTRIES = 400

interface AgentState {
  status: api.AgentStatus | null
  entries: Entry[]
  /** Text streaming in for the current message, before the final block arrives. */
  live: string
  busy: boolean
  /** The half-typed message, kept so navigating away does not lose it. */
  draft: string
  setDraft: (value: string) => void
  send: (message: string, newConversation?: boolean) => Promise<void>
  stop: () => Promise<void>
  newConversation: () => Promise<void>
  setAuto: (enabled: boolean) => Promise<boolean>
  setWeb: (enabled: boolean) => Promise<boolean>
  refreshStatus: (silent?: boolean) => Promise<void>
  /** Answer one proposal. `index` is its position in `entries`. */
  actOnProposal: (index: number, action: ProposalAction) => Promise<void>
}

const AgentContext = createContext<AgentState | null>(null)

/** Lives above the pages, so a turn keeps running - and the transcript keeps
 *  filling - while the user is looking at the calendar. */
export function AgentProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<api.AgentStatus | null>(null)
  const [entries, setEntries] = useState<Entry[]>([])
  const [live, setLive] = useState('')
  const [busy, setBusy] = useState(false)
  const [draft, setDraft] = useState('')

  const abortRef = useRef<AbortController | null>(null)
  // Tool ids already shown, so a repeated block cannot duplicate a row.
  const seenTools = useRef<Set<string>>(new Set())
  // tool_use_id -> the call it belongs to, so a result can be matched to the
  // tool that produced it. Only create_post results become proposals.
  const calls = useRef<Map<string, { name: string; input: Record<string, unknown> }>>(new Map())
  // A mirror of `entries`, so answering a proposal can check its current state
  // without the callback closing over a stale copy.
  const entriesRef = useRef<Entry[]>([])
  // The approval mode, read through a ref: handleEvent is memoised and would
  // otherwise close over whatever the flag happened to be when it was created.
  const autoRef = useRef(false)

  const push = useCallback((entry: Entry) => {
    setEntries(prev => {
      const next = [...prev, entry]
      return next.length > MAX_ENTRIES ? next.slice(next.length - MAX_ENTRIES) : next
    })
  }, [])

  const patchProposal = useCallback((postId: number, changes: Partial<Proposal>) => {
    setEntries(prev => prev.map(entry => (
      entry.kind === 'proposal' && entry.proposal.postId === postId
        ? { ...entry, proposal: { ...entry.proposal, ...changes } }
        : entry
    )))
  }, [])

  /** Watch a post until the browser worker is done, then report what happened.
   *
   *  The outcome is read from the post itself: queuing a publish only means the
   *  browser took the job, so claiming success at that point would be a guess. */
  const settleProposal = useCallback(async (postId: number) => {
    try {
      const post = await api.waitForPost(postId)
      if (post.status === 'posted') patchProposal(postId, { state: 'published' })
      else if (post.status === 'scheduled_on_x') patchProposal(postId, { state: 'scheduled' })
      else patchProposal(postId, { state: 'error', detail: post.error_message || post.status })
    } catch (err) {
      patchProposal(postId, {
        state: 'error',
        detail: err instanceof Error ? err.message : String(err),
      })
    }
  }, [patchProposal])

  /** Send one post on its way. `scheduledAt` picks which kind of yes it is. */
  const publishProposal = useCallback(async (postId: number, scheduledAt: string | null) => {
    if (scheduledAt) await api.scheduleNow(postId)
    else await api.postNow(postId)
    await settleProposal(postId)
  }, [settleProposal])

  /** Follow a post the *agent* published itself, in auto mode.
   *
   *  Without this its card would sit on "waiting for you" for something that has
   *  already gone out. */
  const followAgentPublish = useCallback(async (postId: number) => {
    patchProposal(postId, { state: 'working' })
    await settleProposal(postId)
  }, [patchProposal, settleProposal])

  /** Automatic mode: press the tick for the user, as soon as the post exists.
   *
   *  Done here rather than by instructing the model, because a prompt is advice
   *  the model can decline - and it did, drafting instead of publishing. The
   *  button is the same one the user would press, so there is a single publish
   *  path either way. */
  const autoApprove = useCallback(async (proposal: Proposal) => {
    patchProposal(proposal.postId, { state: 'working', auto: true })
    try {
      await publishProposal(proposal.postId, proposal.scheduledAt)
    } catch (err) {
      patchProposal(proposal.postId, {
        state: 'error',
        auto: true,
        detail: err instanceof Error ? err.message : String(err),
      })
    }
  }, [patchProposal, publishProposal])

  const refreshStatus = useCallback(async () => {
    try {
      setStatus(await api.fetchAgentStatus())
    } catch {
      setStatus(null)
    }
  }, [])

  useEffect(() => { entriesRef.current = entries }, [entries])
  useEffect(() => { autoRef.current = !!status?.auto_approve }, [status])

  useEffect(() => { refreshStatus() }, [refreshStatus])

  // Only fires when the whole app goes away, which is the one time a running
  // turn should be cut short.
  useEffect(() => () => {
    if (abortRef.current) {
      abortRef.current.abort()
      api.stopAgent().catch(() => {})
    }
  }, [])

  const handleEvent = useCallback((event: api.AgentEvent) => {
    // Subagent chatter carries a parent id; keep the transcript to the main thread.
    if (event.parent_tool_use_id) return

    if (event.type === 'stream_event') {
      const delta = event.event?.delta
      if (event.event?.type === 'content_block_delta' && delta?.type === 'text_delta') {
        setLive(prev => prev + (delta.text || ''))
      }
      return
    }

    if (event.type === 'assistant') {
      // The complete message is authoritative; the live buffer was a preview.
      setLive('')
      for (const block of event.message?.content || []) {
        if (block.type === 'text' && block.text?.trim()) {
          push({ kind: 'assistant', text: block.text.trim() })
        } else if (block.type === 'tool_use' && block.name) {
          const id = block.id || `${block.name}:${JSON.stringify(block.input)}`
          if (seenTools.current.has(id)) continue
          seenTools.current.add(id)
          if (block.id) calls.current.set(block.id, { name: block.name, input: block.input || {} })
          push({ kind: 'tool', name: toolLabel(block.name), summary: summariseInput(block.input) })

          // Auto mode: the agent is publishing this one itself, so its card stops
          // asking the user and starts reporting what happened.
          const publishes = /__(publish_now|schedule_on_x)$/.test(block.name)
          const targetId = block.input?.id
          if (publishes && typeof targetId === 'number') followAgentPublish(targetId)
        }
      }
      return
    }

    if (event.type === 'user') {
      // Tool results come back as a user message.
      for (const block of event.message?.content || []) {
        if (block.type !== 'tool_result') continue

        if (block.is_error) {
          const text = typeof block.content === 'string'
            ? block.content
            : JSON.stringify(block.content)
          push({ kind: 'note', tone: 'error', text: text.slice(0, 400) })
          continue
        }

        // A post the agent just created becomes a card the user answers.
        const call = block.tool_use_id ? calls.current.get(block.tool_use_id) : undefined
        if (!call || !call.name.endsWith('__create_post')) continue
        const postId = extractPostId(block.content)
        if (postId === null) continue

        const input = call.input
        const proposal: Proposal = {
          postId,
          text: typeof input.text === 'string' ? input.text : '',
          scheduledAt: typeof input.scheduled_at === 'string' && input.scheduled_at
            ? input.scheduled_at : null,
          media: typeof input.media_path === 'string' && input.media_path
            ? input.media_path.split(/[\\/]/).pop() || null : null,
          state: autoRef.current ? 'working' : 'pending',
          auto: autoRef.current || undefined,
        }
        push({ kind: 'proposal', proposal })
        // Automatic mode: the tick is pressed for the user, right away.
        if (autoRef.current) autoApprove(proposal)
      }
      return
    }

    if (event.type === 'system') {
      if (event.subtype === 'init') {
        const broken = event.mcp_server_errors || []
        const failed = (event.mcp_servers || []).filter(
          s => s.status !== 'connected' && s.status !== 'pending')
        if (broken.length || failed.length) {
          const detail = broken.map(e => `${e.name}: ${e.message}`).join('; ')
          push({ kind: 'note', tone: 'error', code: 'mcpFailed', text: detail || undefined })
        }
      }
      return
    }

    if (event.type === 'result') {
      setLive('')
      if (event.subtype && event.subtype !== 'success') {
        push({
          kind: 'note',
          tone: 'error',
          text: event.result || event.error || String(event.subtype),
        })
      }
      return
    }

    // Our own failure events, for what the CLI never got to report.
    if (event.type === 'xpm' && event.subtype === 'failed') {
      setLive('')
      const text = event.error || ''
      if (text === 'Stopped.') push({ kind: 'note', tone: 'info', code: 'stopped' })
      else push({ kind: 'note', tone: 'error', text: text || undefined })
    }
  }, [push, followAgentPublish, autoApprove])

  const send = useCallback(async (message: string, newConversation = false) => {
    const trimmed = message.trim()
    if (!trimmed || abortRef.current) return

    push({ kind: 'user', text: trimmed })
    setDraft('')
    setLive('')
    setBusy(true)
    seenTools.current.clear()
    calls.current.clear()

    const controller = new AbortController()
    abortRef.current = controller

    try {
      await api.streamAgentChat(trimmed, handleEvent, {
        newConversation,
        signal: controller.signal,
      })
    } catch (err) {
      if (!controller.signal.aborted) {
        push({ kind: 'note', tone: 'error', text: err instanceof Error ? err.message : String(err) })
      }
    } finally {
      abortRef.current = null
      setBusy(false)
      setLive('')
      refreshStatus()
    }
  }, [push, handleEvent, refreshStatus])

  const stop = useCallback(async () => {
    try {
      await api.stopAgent()
    } catch { /* the stream ending is signal enough */ }
    abortRef.current?.abort()
    abortRef.current = null
    setBusy(false)
    setLive('')
    push({ kind: 'note', tone: 'info', code: 'stopped' })
  }, [push])

  const newConversation = useCallback(async () => {
    if (abortRef.current) await stop()
    try {
      await api.resetAgent()
    } catch { /* a fresh transcript is the visible part anyway */ }
    setEntries([])
    setLive('')
    setDraft('')
    seenTools.current.clear()
    calls.current.clear()
    refreshStatus()
  }, [stop, refreshStatus])

  /** The user's yes or no on one proposal.
   *
   *  Goes through the same endpoints the calendar uses, so there is one publish
   *  path in the app rather than a second one for the assistant. */
  const actOnProposal = useCallback(async (index: number, action: ProposalAction) => {
    const patch = (changes: Partial<Proposal>) => setEntries(prev => prev.map((entry, i) => (
      i === index && entry.kind === 'proposal'
        ? { ...entry, proposal: { ...entry.proposal, ...changes } }
        : entry
    )))

    const entry = entriesRef.current[index]
    if (entry?.kind !== 'proposal' || entry.proposal.state !== 'pending') {
      return                      // already answered, or not a proposal
    }

    const postId = entry.proposal.postId
    patch({ state: 'working', detail: undefined })
    try {
      if (action === 'discard') {
        await api.deletePost(postId)
        patch({ state: 'deleted' })
        return
      }
      await publishProposal(postId, action === 'scheduleOnX' ? (entry.proposal.scheduledAt || '') : null)
    } catch (err) {
      patch({ state: 'error', detail: err instanceof Error ? err.message : String(err) })
    }
  }, [publishProposal])

  const setWeb = useCallback(async (enabled: boolean) => {
    try {
      const result = await api.setAgentWeb(enabled)
      setStatus(prev => (prev ? { ...prev, web_access: result.web_access } : prev))
      return true
    } catch {
      return false
    }
  }, [])

  const setAuto = useCallback(async (enabled: boolean) => {
    try {
      const result = await api.setAgentAuto(enabled)
      setStatus(prev => (prev ? { ...prev, auto_approve: result.auto_approve } : prev))
      return true
    } catch {
      return false
    }
  }, [])

  return (
    <AgentContext.Provider value={{
      status, entries, live, busy, draft, setDraft,
      send, stop, newConversation, setAuto, setWeb, refreshStatus, actOnProposal,
    }}>
      {children}
    </AgentContext.Provider>
  )
}

export function useAgent() {
  const ctx = useContext(AgentContext)
  if (!ctx) throw new Error('useAgent must be used within AgentProvider')
  return ctx
}
