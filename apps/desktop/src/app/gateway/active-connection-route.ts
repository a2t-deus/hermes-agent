import type { HermesConnection } from '@/global'
import { $connection } from '@/store/session'

export interface ActiveConnectionRoute {
  connectionId: null | string
  profile: string | undefined
  registryScoped: boolean
}

/** The window route main keys terminal / preview / file IPC on. */
export function activeConnectionRouteOf(connection: HermesConnection | null): ActiveConnectionRoute | null {
  return connection
    ? {
        connectionId: connection.connectionId ?? null,
        profile: connection.profile,
        registryScoped: connection.registryScoped === true
      }
    : null
}

/**
 * Mirror EVERY $connection publication to main's per-window route.
 *
 * Main resolves the embedded terminal's PTY host (local shell vs ssh) from
 * this route, not from the renderer atom. Reporting it only from the boot
 * hook's publish() missed store-driven switches that call setConnection
 * directly — notably Sessions switcher → back to the local primary, where
 * ensureGatewayAgent publishes the descriptor itself. Main then kept the
 * previous SSH source's route and new terminals opened on that remote host
 * while the session list showed this device.
 */
export function mirrorActiveConnectionRoute(report: (route: ActiveConnectionRoute | null) => void): () => void {
  return $connection.subscribe(connection => report(activeConnectionRouteOf(connection)))
}
