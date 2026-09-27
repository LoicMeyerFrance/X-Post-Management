/** Formatting helpers for the assistant transcript.
 *
 *  Pure functions, kept out of the context file so that one only exports
 *  components and hooks. */

/** Pull the new post's id out of a create_post tool result.
 *
 *  The MCP server answers with JSON, but a tool result reaches us as a string,
 *  as an array of content blocks, or already parsed, depending on the client. All
 *  three are flattened to text and parsed; `null` means "no id in there", which
 *  the caller treats as "nothing to offer the user". */
export function extractPostId(content: unknown): number | null {
  const text = typeof content === 'string'
    ? content
    : Array.isArray(content)
      ? content.map(block => {
          if (typeof block === 'string') return block
          if (block && typeof block === 'object' && 'text' in block) {
            return String((block as { text?: unknown }).text ?? '')
          }
          return ''
        }).join('\n')
      : content && typeof content === 'object'
        ? JSON.stringify(content)
        : ''

  if (!text) return null
  try {
    const parsed = JSON.parse(text)
    if (parsed && typeof parsed === 'object' && typeof (parsed as { id?: unknown }).id === 'number') {
      return (parsed as { id: number }).id
    }
  } catch {
    // Not a bare JSON document; fall through to the scan below.
  }
  const match = text.match(/"id"\s*:\s*(\d+)/)
  return match ? Number(match[1]) : null
}

/** Strip the mcp__server__ prefix so the transcript reads as plain words. */
export function toolLabel(name: string): string {
  return name.replace(/^mcp__[^_]+__/, '').replace(/_/g, ' ')
}

/** A one-line gist of a tool's arguments, for the transcript. */
export function summariseInput(input: Record<string, unknown> | undefined): string {
  if (!input) return ''
  const parts: string[] = []
  if (typeof input.id === 'number') parts.push(`#${input.id}`)
  if (typeof input.status === 'string' && input.status) parts.push(input.status)
  if (typeof input.scheduled_at === 'string' && input.scheduled_at) {
    parts.push(input.scheduled_at.replace('T', ' '))
  }
  if (typeof input.media_path === 'string' && input.media_path) {
    parts.push(input.media_path.split(/[\\/]/).pop() || 'media')
  }
  if (typeof input.text === 'string' && input.text) {
    const clean = input.text.replace(/\s+/g, ' ').trim()
    parts.push(`"${clean.length > 60 ? clean.slice(0, 60) + '…' : clean}"`)
  }
  return parts.join(' · ')
}
