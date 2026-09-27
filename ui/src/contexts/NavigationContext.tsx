import { createContext, useContext, useState, useCallback, type ReactNode } from 'react'

export type Page =
  | 'composer' | 'schedule' | 'calendar' | 'history'
  | 'logs' | 'settings' | 'profile' | 'agent' | 'about'

interface NavigationState {
  /** Go to a page, optionally asking it to reveal one of its sections. */
  goTo: (page: Page, section?: string) => void
  /** Section the destination page should scroll to, once. */
  pendingSection: string | null
  clearPendingSection: () => void
}

const NavigationContext = createContext<NavigationState | null>(null)

export function NavigationProvider({
  onNavigate,
  children,
}: {
  onNavigate: (page: Page) => void
  children: ReactNode
}) {
  const [pendingSection, setPendingSection] = useState<string | null>(null)

  const goTo = useCallback((page: Page, section?: string) => {
    setPendingSection(section ?? null)
    onNavigate(page)
  }, [onNavigate])

  const clearPendingSection = useCallback(() => setPendingSection(null), [])

  return (
    <NavigationContext.Provider value={{ goTo, pendingSection, clearPendingSection }}>
      {children}
    </NavigationContext.Provider>
  )
}

export function useNavigation() {
  const ctx = useContext(NavigationContext)
  if (!ctx) throw new Error('useNavigation must be used within NavigationProvider')
  return ctx
}
