import { type ClassValue, clsx } from "clsx"
import { twMerge } from "tailwind-merge"
import { toast } from 'sonner'
import type { Locale, TranslationKey } from './i18n'

type ActionResult = { success: boolean; error?: string }

/** Read the outcome off a post once the browser worker has finished with it. */
export function outcomeOf(
  post: { status: string; error_message: string | null },
  expected: string,
): ActionResult {
  return {
    success: post.status === expected,
    error: post.error_message || undefined,
  }
}

/** Report the outcome of a bot action, surfacing the server's own error
 *  message rather than a generic one. */
export function toastResult(
  result: ActionResult,
  successMessage: string,
  t: (key: TranslationKey) => string,
) {
  if (result.success) {
    toast.success(successMessage)
  } else {
    toast.error(`${t('common.errorPrefix')} : ${result.error || t('common.unknownError')}`)
  }
}

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function formatDate(isoStr: string, locale: Locale = 'fr'): string {
  if (!isoStr) return ''
  try {
    const d = new Date(isoStr)
    if (isNaN(d.getTime())) return isoStr
    return d.toLocaleString(locale === 'fr' ? 'fr-FR' : 'en-US', {
      day: '2-digit', month: '2-digit', year: 'numeric',
      hour: '2-digit', minute: '2-digit'
    })
  } catch {
    return isoStr
  }
}

export function timeFromNow(isoStr: string, locale: Locale = 'fr'): string {
  const diff = new Date(isoStr).getTime() - Date.now()
  if (diff <= 0) return locale === 'fr' ? 'maintenant' : 'now'
  const mins = Math.round(diff / 60000)
  if (mins < 60) return locale === 'fr' ? `dans ${mins}min` : `in ${mins}min`
  const hours = Math.round(mins / 60)
  if (mins < 1440) return locale === 'fr' ? `dans ${hours}h` : `in ${hours}h`
  const days = Math.round(mins / 1440)
  return locale === 'fr' ? `dans ${days}j` : `in ${days}d`
}
