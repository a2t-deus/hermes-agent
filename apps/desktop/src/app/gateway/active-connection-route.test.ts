import { afterEach, describe, expect, it } from 'vitest'

import type { HermesConnection } from '@/global'
import { setConnection } from '@/store/session'

import { type ActiveConnectionRoute, mirrorActiveConnectionRoute } from './active-connection-route'

function descriptor(connectionId: string, mode: 'local' | 'remote'): HermesConnection {
  return {
    baseUrl: 'http://127.0.0.1:1',
    connectionId,
    mode,
    profile: 'default',
    registryScoped: true,
    token: 't',
    wsUrl: 'ws://127.0.0.1:1'
  } as unknown as HermesConnection
}

describe('mirrorActiveConnectionRoute', () => {
  afterEach(() => setConnection(null))

  it('re-points main at local after a store-driven switch back from an SSH source', () => {
    const routes: (ActiveConnectionRoute | null)[] = []
    const off = mirrorActiveConnectionRoute(route => routes.push(route))

    // local → MBP 14 (ssh) → local, each published straight through
    // setConnection as ensureGatewayAgent does (no boot-hook publish()).
    setConnection(descriptor('local', 'local'))
    setConnection(descriptor('blanket14', 'remote'))
    setConnection(descriptor('local', 'local'))
    off()

    expect(routes.at(-1)).toEqual({ connectionId: 'local', profile: 'default', registryScoped: true })
    expect(routes.map(route => route?.connectionId ?? null)).toEqual([null, 'local', 'blanket14', 'local'])
  })

  it('stops reporting once unsubscribed', () => {
    const routes: (ActiveConnectionRoute | null)[] = []
    mirrorActiveConnectionRoute(route => routes.push(route))()
    setConnection(descriptor('blanket14', 'remote'))

    expect(routes).toEqual([null])
  })
})
