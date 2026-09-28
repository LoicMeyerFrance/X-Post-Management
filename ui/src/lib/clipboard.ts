/** Copying text out of the app.
 *
 *  The desktop shell disables the browser's own copy shortcut and its right-click
 *  menu: pywebview sets `AreBrowserAcceleratorKeysEnabled` and
 *  `AreDefaultContextMenusEnabled` from its debug flag, so in a release build
 *  Ctrl+C does nothing and there is no menu to copy from. Selecting text works
 *  (that is a separate setting) but there is no way to take it anywhere.
 *
 *  So the app copies for itself. */

/** Put text on the clipboard. Returns false when the platform refuses. */
export async function copyText(text: string): Promise<boolean> {
  if (!text) return false
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    // Older shells, or a context the Clipboard API considers insecure.
    try {
      const area = document.createElement('textarea')
      area.value = text
      area.setAttribute('readonly', '')
      area.style.position = 'fixed'
      area.style.opacity = '0'
      document.body.appendChild(area)
      area.select()
      const ok = document.execCommand('copy')
      document.body.removeChild(area)
      return ok
    } catch {
      return false
    }
  }
}

/** Make Ctrl+C / Cmd+C copy the selection, and return the teardown.
 *
 *  Deliberately does not call preventDefault: where the native shortcut still
 *  works — a real browser, or a shell that left it enabled — both paths write the
 *  same text and the second is a no-op. Suppressing the native one would break
 *  copying inside inputs, where the browser already does the right thing. */
export function installCopyShortcut(): () => void {
  const onKeyDown = (event: KeyboardEvent) => {
    if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== 'c') return
    if (event.shiftKey || event.altKey) return

    const target = event.target as HTMLElement | null
    // An input or textarea handles its own selection; leave it alone.
    const tag = target?.tagName
    if (tag === 'INPUT' || tag === 'TEXTAREA' || target?.isContentEditable) return

    const selected = window.getSelection()?.toString() ?? ''
    if (selected.trim()) copyText(selected)
  }

  window.addEventListener('keydown', onKeyDown)
  return () => window.removeEventListener('keydown', onKeyDown)
}
