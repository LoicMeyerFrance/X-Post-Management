import { useState, useEffect } from 'react'
import { RefreshCw, Loader2, BadgeCheck, Users, UserPlus, ExternalLink, CalendarDays, TrendingUp, TrendingDown } from 'lucide-react'
import { toast } from 'sonner'
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'
import { PageHeader } from '@/components/PageHeader'
import { useSettings } from '@/contexts/SettingsContext'
import * as api from '@/lib/api'

export function Profile() {
  const { t, locale } = useSettings()
  const [stats, setStats] = useState<api.ProfileStats | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)

  const loadStats = async () => {
    try {
      const data = await api.fetchProfileStats()
      setStats(data)
    } catch { /* ignore */ }
    finally { setLoading(false) }
  }

  useEffect(() => { loadStats() }, [])

  const handleRefresh = async () => {
    setRefreshing(true)
    try {
      const result = await api.fetchProfileFromX()
      if (result.success) {
        toast.success(t('profile.refreshed'))
        const data = await api.fetchProfileStats()
        setStats(data)
      } else {
        toast.error(result.error || t('common.unknownError'))
      }
    } catch {
      toast.error(t('common.serverError'))
    } finally {
      setRefreshing(false)
    }
  }

  const formatNumber = (n: number): string => {
    if (n >= 1000000) return (n / 1000000).toFixed(1).replace(/\.0$/, '') + 'M'
    if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'K'
    return n.toString()
  }

  // Compute follower variation from history
  const history = stats?.history || []
  let followersDiff: number | null = null
  if (history.length >= 2) {
    const latest = history[history.length - 1].followers_count
    const previous = history[history.length - 2].followers_count
    followersDiff = latest - previous
  }

  const chartData = history.map(h => ({
    date: new Date(h.recorded_at).toLocaleDateString(locale === 'fr' ? 'fr-FR' : 'en-US', { month: 'short', day: 'numeric' }),
    followers: h.followers_count,
    following: h.following_count,
  }))

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 size={24} className="animate-spin text-text-muted" />
      </div>
    )
  }

  const profile = stats?.profile

  return (
    <div>
      <PageHeader title={t('profile.title')} description={t('profile.desc')} />

      {/* Profile card */}
      <div className="px-6 py-5 border-b border-border">
        <div className="flex items-start gap-4 p-5 bg-bg-secondary/50 rounded-xl border border-border">
          <img
            src={api.profilePictureUrl()}
            alt=""
            className="w-16 h-16 rounded-full bg-bg-secondary object-cover border-2 border-border shrink-0"
            onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
          />
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <span className="text-lg font-semibold text-text">{profile?.display_name || '-'}</span>
              {profile?.is_verified && (
                <BadgeCheck size={18} className={
                  profile.verified_type === 'business' ? 'text-[#E0A526]' :
                  profile.verified_type === 'government' ? 'text-[#829AAB]' :
                  'text-[#1D9BF0]'
                } />
              )}
            </div>
            <div className="text-sm text-text-muted">{profile?.username ? `@${profile.username}` : '-'}</div>

            {/* Bio */}
            {profile?.bio && (
              <p className="text-sm text-text mt-2 whitespace-pre-wrap">{profile.bio}</p>
            )}

            {/* Join date + View on X */}
            <div className="flex items-center gap-4 mt-2">
              {profile?.join_date && (
                <div className="flex items-center gap-1.5 text-xs text-text-muted">
                  <CalendarDays size={13} />
                  <span>{profile.join_date}</span>
                </div>
              )}
              {profile?.username && (
                <a
                  href={`https://x.com/${profile.username}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1.5 text-xs text-accent hover:underline"
                >
                  {t('profile.viewOnX')}
                  <ExternalLink size={11} />
                </a>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Stats */}
      <div className="px-6 py-5 border-b border-border">
        <div className="grid grid-cols-2 gap-4 max-w-sm">
          <div className="flex items-center gap-3 px-4 py-3 bg-bg-secondary rounded-lg border border-border">
            <Users size={24} className="text-accent shrink-0" />
            <div>
              <div className="flex items-baseline gap-2">
                <span className="text-xl font-bold text-text">{formatNumber(profile?.followers_count || 0)}</span>
                {followersDiff !== null && followersDiff !== 0 && (
                  <span className={`inline-flex items-center gap-0.5 text-xs font-medium ${followersDiff > 0 ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}`}>
                    {followersDiff > 0 ? <TrendingUp size={12} /> : <TrendingDown size={12} />}
                    {followersDiff > 0 ? '+' : ''}{formatNumber(followersDiff)}
                  </span>
                )}
              </div>
              <div className="text-xs text-text-muted">{t('profile.followers')}</div>
              {followersDiff !== null && followersDiff !== 0 && (
                <div className="text-[10px] text-text-muted mt-0.5">{t('profile.variation')}</div>
              )}
            </div>
          </div>
          <div className="flex items-center gap-3 px-4 py-3 bg-bg-secondary rounded-lg border border-border">
            <UserPlus size={24} className="text-accent shrink-0" />
            <div>
              <div className="text-xl font-bold text-text">{formatNumber(profile?.following_count || 0)}</div>
              <div className="text-xs text-text-muted">{t('profile.following')}</div>
            </div>
          </div>
        </div>
      </div>

      {/* Growth chart */}
      <div className="px-6 py-5 border-b border-border">
        <h3 className="text-sm font-semibold text-text mb-4">{t('profile.growth')}</h3>
        {chartData.length >= 2 ? (
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={chartData}>
                <defs>
                  <linearGradient id="colorFollowers" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="var(--color-accent, #1D9BF0)" stopOpacity={0.3} />
                    <stop offset="95%" stopColor="var(--color-accent, #1D9BF0)" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border, #e5e7eb)" />
                <XAxis dataKey="date" tick={{ fontSize: 11 }} stroke="var(--color-text-muted, #9ca3af)" />
                <YAxis tick={{ fontSize: 11 }} stroke="var(--color-text-muted, #9ca3af)" tickFormatter={formatNumber} />
                <Tooltip
                  contentStyle={{
                    backgroundColor: 'var(--color-bg, #fff)',
                    border: '1px solid var(--color-border, #e5e7eb)',
                    borderRadius: '8px',
                    fontSize: '12px',
                  }}
                  formatter={(value: number | undefined) => [formatNumber(value ?? 0), t('profile.followers')]}
                />
                <Area
                  type="monotone"
                  dataKey="followers"
                  stroke="var(--color-accent, #1D9BF0)"
                  strokeWidth={2}
                  fillOpacity={1}
                  fill="url(#colorFollowers)"
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <p className="text-sm text-text-muted py-8 text-center">
            {chartData.length === 1 ? t('profile.needMoreData') : t('profile.noData')}
          </p>
        )}
      </div>

      {/* Refresh button */}
      <div className="px-6 py-5">
        <button
          onClick={handleRefresh}
          disabled={refreshing}
          className="inline-flex items-center gap-2 px-4 py-2.5 text-sm font-medium text-white bg-accent rounded-lg hover:bg-accent-hover transition-colors disabled:opacity-50 shadow-sm"
        >
          {refreshing ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
          {refreshing ? t('profile.refreshing') : t('profile.refreshBtn')}
        </button>
      </div>
    </div>
  )
}
