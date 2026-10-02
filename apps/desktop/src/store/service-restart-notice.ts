import { playCompletionSound } from '@/lib/completion-sound'
import { dispatchNativeNotification } from '@/store/native-notifications'
import { notify } from '@/store/notifications'

// A restart can surface on several sockets/windows for the same turn; announce
// each cut turn once.
const DEDUPE_MS = 10_000
const recentlyAnnounced = new Map<string, number>()

/**
 * The Hermes serve backend restarted (WS close 1012) while these turns were
 * running. The backend does NOT resume them, so tell the user plainly.
 */
export function notifyTurnsInterruptedByRestart(sessionIds: readonly string[]): void {
  const now = Date.now()

  for (const [id, at] of recentlyAnnounced) {
    if (now - at >= DEDUPE_MS) {
      recentlyAnnounced.delete(id)
    }
  }

  const ids = sessionIds.filter(id => id && !recentlyAnnounced.has(id))

  if (ids.length === 0) {
    return
  }

  for (const id of ids) {
    recentlyAnnounced.set(id, now)
  }

  const count = ids.length
  const title = 'Turn interrupted — Hermes restarted'

  const body = `${count} running ${count === 1 ? 'turn was' : 'turns were'} stopped by the backend restart. Nothing was resumed — send "continue" to pick up.`

  notify({ kind: 'warning', title, message: body, durationMs: 15_000 })
  playCompletionSound(`restart:${ids[0]}`)
  dispatchNativeNotification({ kind: 'turnError', global: true, tag: 'serve-restart', sessionId: ids[0], title, body })
}
