const BASE = ''

class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function handleResponse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText)
    throw new ApiError(res.status, text || `HTTP ${res.status}`)
  }
  return res.json()
}

export interface Post {
  id: number
  text: string
  image_path: string
  scheduled_at: string | null
  status: 'draft' | 'scheduled' | 'scheduling' | 'scheduled_on_x' | 'posting' | 'posted' | 'error'
  created_at: string
  updated_at: string
  posted_at: string | null
  error_message: string | null
  retries_count: number
  tweet_url: string | null
}

/** X's video ceiling, which depends on the account. */
export const MAX_VIDEO_BYTES_STANDARD = 512 * 1024 * 1024
export const MAX_VIDEO_BYTES_PREMIUM = 16 * 1024 * 1024 * 1024

export interface Profile {
  display_name: string
  username: string
  has_picture: boolean
  is_verified: boolean
  verified_type: '' | 'blue' | 'business' | 'government'
  followers_count: number
  following_count: number
  bio: string
  join_date: string
}

export interface FollowerSnapshot {
  id: number
  followers_count: number
  following_count: number
  recorded_at: string
}

export interface ProfileStats {
  profile: Omit<Profile, 'has_picture'>
  history: FollowerSnapshot[]
}

/** Fetch every post, or only those in the given status(es) - the server
 *  accepts a comma-separated list, so one round-trip covers several. */
export async function fetchPosts(status?: string | string[]): Promise<Post[]> {
  const statuses = Array.isArray(status) ? status.join(',') : status
  const url = statuses
    ? `${BASE}/api/posts?status=${encodeURIComponent(statuses)}`
    : `${BASE}/api/posts`
  const res = await fetch(url)
  return handleResponse<Post[]>(res)
}

export async function fetchPost(id: number): Promise<Post> {
  const res = await fetch(`${BASE}/api/posts/${id}`)
  return handleResponse<Post>(res)
}

export async function createPost(formData: FormData): Promise<Post & { error?: string }> {
  const res = await fetch(`${BASE}/api/posts`, { method: 'POST', body: formData })
  return handleResponse<Post & { error?: string }>(res)
}

export async function updatePost(id: number, data: Partial<Post>): Promise<Post> {
  const res = await fetch(`${BASE}/api/posts/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  return handleResponse<Post>(res)
}

export async function deletePost(id: number): Promise<void> {
  const res = await fetch(`${BASE}/api/posts/${id}`, { method: 'DELETE' })
  if (!res.ok) throw new ApiError(res.status, res.statusText)
}

/** The server hands the work to the browser and answers at once; the outcome
 *  shows up on the post itself. */
export interface Queued {
  queued: boolean
  id: number
  status: Post['status']
}

export async function postNow(id: number): Promise<Queued> {
  const res = await fetch(`${BASE}/api/posts/${id}/post-now`, { method: 'POST' })
  return handleResponse<Queued>(res)
}

export async function scheduleNow(id: number): Promise<Queued> {
  const res = await fetch(`${BASE}/api/posts/${id}/schedule-now`, { method: 'POST' })
  return handleResponse<Queued>(res)
}

export async function retryPost(id: number): Promise<Queued> {
  const res = await fetch(`${BASE}/api/posts/${id}/retry`, { method: 'POST' })
  return handleResponse<Queued>(res)
}

/** Statuses the browser worker is still holding the post in. */
const IN_FLIGHT: Post['status'][] = ['posting', 'scheduling']

/** Watch a post until the browser worker is finished with it. */
export async function waitForPost(
  id: number,
  { timeoutMs = 360000, intervalMs = 1500 } = {},
): Promise<Post> {
  const deadline = Date.now() + timeoutMs
  let post = await fetchPost(id)
  while (IN_FLIGHT.includes(post.status) && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, intervalMs))
    post = await fetchPost(id)
  }
  return post
}

export async function removeMedia(id: number): Promise<{ success: boolean }> {
  const res = await fetch(`${BASE}/api/posts/${id}/remove-media`, { method: 'POST' })
  return handleResponse<{ success: boolean }>(res)
}

export async function duplicatePost(id: number): Promise<Post> {
  const res = await fetch(`${BASE}/api/posts/${id}/duplicate`, { method: 'POST' })
  return handleResponse<Post>(res)
}

export interface CheckOnXResult {
  checked: number
  missing: { id: number; text: string }[]
  unknown: number
  truncated: boolean
  error?: string
}

/** Ask X which of the posts we list still exist there. Slow: it opens each one. */
export async function checkPostsOnX(): Promise<CheckOnXResult> {
  const res = await fetch(`${BASE}/api/posts/check-on-x`, { method: 'POST' })
  return handleResponse<CheckOnXResult>(res)
}

export async function deleteFromX(id: number): Promise<{ success: boolean; error?: string; already_deleted?: boolean }> {
  const res = await fetch(`${BASE}/api/posts/${id}/delete-from-x`, { method: 'POST' })
  return handleResponse<{ success: boolean; error?: string; already_deleted?: boolean }>(res)
}

export async function deleteScheduledFromX(id: number): Promise<{ success: boolean; error?: string }> {
  const res = await fetch(`${BASE}/api/posts/${id}/delete-scheduled-from-x`, { method: 'POST' })
  return handleResponse<{ success: boolean; error?: string }>(res)
}

export async function testConnection(): Promise<{ success: boolean; error?: string; needs_manual_intervention?: boolean }> {
  const res = await fetch(`${BASE}/api/settings/test-connection`)
  return handleResponse<{ success: boolean; error?: string; needs_manual_intervention?: boolean }>(res)
}

export async function fetchProfile(): Promise<Profile> {
  const res = await fetch(`${BASE}/api/profile`)
  return handleResponse<Profile>(res)
}

export async function fetchProfileFromX(): Promise<{ success: boolean; display_name?: string; username?: string; error?: string }> {
  const res = await fetch(`${BASE}/api/profile/fetch`, { method: 'POST' })
  return handleResponse<{ success: boolean; display_name?: string; username?: string; error?: string }>(res)
}

export async function fetchProfileStats(): Promise<ProfileStats> {
  const res = await fetch(`${BASE}/api/profile/stats`)
  return handleResponse<ProfileStats>(res)
}

export interface LogsResult {
  logs?: string
  fingerprint?: string
  unchanged?: boolean
}

/** Pass the fingerprint you already hold to skip re-downloading the same tail. */
export async function fetchLogs(fingerprint?: string): Promise<LogsResult> {
  const query = fingerprint ? `?fingerprint=${encodeURIComponent(fingerprint)}` : ''
  const res = await fetch(`${BASE}/api/logs${query}`)
  return handleResponse<LogsResult>(res)
}

export async function fetchPreferences(): Promise<Record<string, string>> {
  const res = await fetch(`${BASE}/api/settings/preferences`)
  return handleResponse<Record<string, string>>(res)
}

export async function savePreferences(data: Record<string, string>): Promise<{ success: boolean }> {
  const res = await fetch(`${BASE}/api/settings/preferences`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  return handleResponse<{ success: boolean }>(res)
}

export async function fetchEnvSettings(): Promise<Record<string, string>> {
  const res = await fetch(`${BASE}/api/settings/env`)
  return handleResponse<Record<string, string>>(res)
}

export async function saveEnvSettings(data: Record<string, string>): Promise<{ success?: boolean; error?: string }> {
  const res = await fetch(`${BASE}/api/settings/env`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  return handleResponse<{ success?: boolean; error?: string }>(res)
}

export function profilePictureUrl(): string {
  return `${BASE}/api/profile/picture?t=${Math.floor(Date.now() / 60000)}`
}

export function uploadUrl(filename: string): string {
  return `${BASE}/uploads/${filename}`
}

export async function browseFolder(): Promise<{ path: string | null; error?: string }> {
  const res = await fetch(`${BASE}/api/browse/folder`, { method: 'POST' })
  return handleResponse<{ path: string | null; error?: string }>(res)
}

export async function browseFile(): Promise<{ path: string | null; error?: string }> {
  const res = await fetch(`${BASE}/api/browse/file`, { method: 'POST' })
  return handleResponse<{ path: string | null; error?: string }>(res)
}

export async function detectChrome(): Promise<{ chrome_path: string | null; profile_dir: string | null; detected: boolean }> {
  const res = await fetch(`${BASE}/api/detect-chrome`)
  return handleResponse<{ chrome_path: string | null; profile_dir: string | null; detected: boolean }>(res)
}

export interface ConnectResult {
  success: boolean
  error?: string
  message?: string
  rate_limited?: boolean
}

/** Sign in to X in a visible window. Long-running: X may ask for a code, and
 *  the server waits up to 5 minutes for the user to answer it. */
export async function connectX(): Promise<ConnectResult> {
  const res = await fetch(`${BASE}/api/settings/connect-x`, { method: 'POST' })
  return handleResponse<ConnectResult>(res)
}

export interface ConnectionStatus {
  connected: boolean
  username?: string
  checked_at?: string
  reason?: string
  error?: string
}

/** Last known connection state. Cheap - never opens a browser. */
export async function fetchConnectionStatus(): Promise<ConnectionStatus> {
  const res = await fetch(`${BASE}/api/settings/connection-status`)
  return handleResponse<ConnectionStatus>(res)
}
