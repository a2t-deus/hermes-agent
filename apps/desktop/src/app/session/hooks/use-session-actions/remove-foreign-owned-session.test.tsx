// Invariant: a listed, untagged, profile-less row whose owner the resolver
// finds on ANOTHER registry connection is deleted/archived on that connection
// (or fails closed) — never by a bare profile on the ambient connection. A bare
// 'default' there hits the Mini's own state.db, answers already_absent, and the
// row reappears on refresh (review 107, B1).
import { act, cleanup, render, waitFor } from '@testing-library/react'
import type { MutableRefObject } from 'react'
import { useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setApiRequestConnection } from '@/api/client'
import { deleteSession, getSession, type SessionInfo, setSessionArchived } from '@/hermes'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $profiles } from '@/store/profile'
import { setSessions } from '@/store/session'

import type { ClientSessionState } from '../../../types'

import { useSessionActions } from './index'

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  deleteSession: vi.fn(),
  getSession: vi.fn(),
  getAllSessionMessages: vi.fn(),
  getLatestSessionMessages: vi.fn(),
  listAllProfileSessions: vi.fn(),
  setApiRequestProfile: vi.fn(),
  setSessionArchived: vi.fn()
}))

vi.mock('@/store/profile', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ensureGatewayProfile: vi.fn().mockResolvedValue(undefined)
}))

const SID = '20260914_131543_01d58a'

function untaggedRow(): SessionInfo {
  return {
    ended_at: null,
    id: SID,
    input_tokens: 0,
    is_active: false,
    last_active: 1,
    message_count: 2,
    model: null,
    output_tokens: 0,
    preview: null,
    source: 'desktop',
    started_at: 1,
    title: 'laptop-owned',
    tool_call_count: 0
  } as SessionInfo
}

type Handle = Pick<ReturnType<typeof useSessionActions>, 'archiveSession' | 'removeSession'>

function Harness({ onReady }: { onReady: (handle: Handle) => void }) {
  const ref = <T,>(value: T): MutableRefObject<T> => ({ current: value })

  const actions = useSessionActions({
    activeSessionId: null,
    activeSessionIdRef: ref<string | null>(null),
    busyRef: ref(false),
    creatingSessionRef: ref(false),
    ensureSessionState: () => ({}) as ClientSessionState,
    getRouteToken: () => 'token',
    getRoutedStoredSessionId: () => null,
    navigate: vi.fn() as never,
    requestGateway: vi.fn().mockResolvedValue(undefined),
    resetViewSync: vi.fn(),
    runtimeIdByStoredSessionIdRef: ref(new Map<string, string>()),
    selectedStoredSessionId: null,
    selectedStoredSessionIdRef: ref<string | null>(null),
    sessionStateByRuntimeIdRef: ref(new Map<string, ClientSessionState>()),
    syncSessionStateToView: vi.fn(),
    updateSessionState: () => ({}) as ClientSessionState
  })

  useEffect(() => {
    onReady({ archiveSession: actions.archiveSession, removeSession: actions.removeSession })
  }, [actions, onReady])

  return null
}

async function mountHarness(): Promise<Handle> {
  let handle: Handle | undefined
  render(<Harness onReady={h => (handle = h)} />)
  await waitFor(() => expect(handle).toBeDefined())

  return handle as Handle
}

function expectOwnerRouteOrFailClosed(calls: unknown[][]) {
  for (const [, owner] of calls) {
    expect(owner).toMatchObject({ connectionId: 'laptop-tailnet' })
  }
}

describe('delete/archive of a session owned by another registry connection', () => {
  beforeEach(() => {
    // Window's active source is the Mini; the session lives on the laptop.
    setApiRequestConnection('mini-tailnet')
    $connectionsRegistry.set({
      primary: 'mini-tailnet',
      connections: [{ id: 'local' }, { id: 'mini-tailnet' }, { id: 'laptop-tailnet' }]
    } as never)
    $profiles.set([{ name: 'default' }, { name: 'a' }, { name: 'b' }] as never)
    setSessions([untaggedRow()])
    vi.mocked(getSession).mockImplementation(async (id, scope) => {
      if (id === SID && typeof scope === 'object' && scope?.connectionId === 'laptop-tailnet') {
        return untaggedRow()
      }

      throw Object.assign(new Error('404 session not found'), { status: 404 })
    })
    vi.mocked(deleteSession).mockResolvedValue({ ok: true })
    vi.mocked(setSessionArchived).mockResolvedValue({ ok: true })
  })

  afterEach(() => {
    cleanup()
    setSessions([])
    setApiRequestConnection(null)
    $connectionsRegistry.set(null)
    $profiles.set([])
    vi.clearAllMocks()
  })

  it('deletes on the owner connection, never by bare ambient profile', async () => {
    const handle = await mountHarness()

    await act(() => handle.removeSession(SID))

    expect(deleteSession).not.toHaveBeenCalledWith(SID, 'default')
    expectOwnerRouteOrFailClosed(vi.mocked(deleteSession).mock.calls)
  })

  it('archives on the owner connection, never by bare ambient profile', async () => {
    const handle = await mountHarness()

    await act(() => handle.archiveSession(SID))

    expect(setSessionArchived).not.toHaveBeenCalledWith(SID, true, 'default')
    expectOwnerRouteOrFailClosed(vi.mocked(setSessionArchived).mock.calls.map(([id, , owner]) => [id, owner]))
  })
})
