import { mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  applySetCookie,
  cookieHeader,
  mintWsTicket,
  readCookieJar,
  RemoteAuthExpiredError,
  remoteGatewayWsUrl,
  resolveRemoteAttach,
  ticketSubprotocols,
  writeCookieJar
} from '../remoteAuth.js'

let dir: string
let cookieFile: string

const jsonResponse = (body: unknown, init: ResponseInit = {}) =>
  new Response(JSON.stringify(body), {
    ...init,
    headers: { 'content-type': 'application/json', ...(init.headers ?? {}) }
  })

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'hermes-remote-auth-'))
  cookieFile = join(dir, 'cookies.json')
})

afterEach(() => {
  rmSync(dir, { force: true, recursive: true })
})

describe('resolveRemoteAttach', () => {
  it('is null unless BOTH the url and the cookie file are present', () => {
    expect(resolveRemoteAttach({})).toBeNull()
    expect(resolveRemoteAttach({ HERMES_TUI_REMOTE_URL: 'http://a.test:1' })).toBeNull()
    expect(resolveRemoteAttach({ HERMES_TUI_REMOTE_COOKIE_FILE: '/tmp/c.json' })).toBeNull()
  })

  it('normalizes a trailing slash and falls back to the url as a display name', () => {
    const cfg = resolveRemoteAttach({
      HERMES_TUI_REMOTE_COOKIE_FILE: '/tmp/c.json',
      HERMES_TUI_REMOTE_URL: 'http://mini.test:9129/'
    })

    expect(cfg).toEqual({
      baseUrl: 'http://mini.test:9129',
      cookieFile: '/tmp/c.json',
      name: 'http://mini.test:9129'
    })
  })
})

describe('remoteGatewayWsUrl', () => {
  it('maps http to ws and https to wss on the gateway path', () => {
    expect(remoteGatewayWsUrl('http://mini.test:9129')).toBe('ws://mini.test:9129/api/ws')
    expect(remoteGatewayWsUrl('https://mini.test/')).toBe('wss://mini.test/api/ws')
  })
})

describe('cookie jar', () => {
  it('reads a missing or corrupt jar as empty rather than throwing at the user', () => {
    expect(readCookieJar(join(dir, 'nope.json')).cookies).toEqual({})
    writeFileSync(cookieFile, 'not json {{')
    expect(readCookieJar(cookieFile).cookies).toEqual({})
  })

  it('round-trips 0600 so a shared box cannot read a live session', () => {
    writeCookieJar(cookieFile, { cookies: { hermes_session_at: 'at-1' }, username: 'sagi' })

    expect(statSync(cookieFile).mode & 0o777).toBe(0o600)
    expect(readCookieJar(cookieFile)).toMatchObject({
      cookies: { hermes_session_at: 'at-1' },
      username: 'sagi'
    })
    expect(JSON.parse(readFileSync(cookieFile, 'utf8')).version).toBe(1)
  })

  it('renders the jar as one Cookie header', () => {
    expect(cookieHeader({ cookies: { a: '1', b: '2' } })).toBe('a=1; b=2')
    expect(cookieHeader({ cookies: {} })).toBe('')
  })
})

describe('applySetCookie', () => {
  it('merges rotated values and keeps untouched cookies', () => {
    const jar = { cookies: { hermes_session_at: 'old', hermes_session_provider: 'local' } }
    const next = applySetCookie(jar, ['hermes_session_at=new; Path=/; HttpOnly; SameSite=Lax'])

    expect(next.cookies).toEqual({ hermes_session_at: 'new', hermes_session_provider: 'local' })
  })

  it('honours Max-Age=0 as a deletion', () => {
    const jar = { cookies: { hermes_session_at: 'old', hermes_session_rt: 'r' } }
    const next = applySetCookie(jar, ['hermes_session_at=; Max-Age=0; Path=/'])

    expect(next.cookies).toEqual({ hermes_session_rt: 'r' })
  })

  it('ignores malformed header lines instead of writing a junk cookie', () => {
    const next = applySetCookie({ cookies: { keep: '1' } }, ['', '=novalue', 'nonsense'])

    expect(next.cookies).toEqual({ keep: '1' })
  })
})

describe('mintWsTicket', () => {
  const remote = { baseUrl: 'http://mini.test:9129', cookieFile: '', name: 'mini' }

  it('sends the stored cookies and returns the ticket', async () => {
    writeCookieJar(cookieFile, { cookies: { hermes_session_at: 'at-1' } })
    const fetchImpl = vi.fn(async () => jsonResponse({ ticket: 't-abc', ttl_seconds: 30 }))

    const ticket = await mintWsTicket({ ...remote, cookieFile }, fetchImpl as unknown as typeof fetch)

    expect(ticket).toBe('t-abc')
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('http://mini.test:9129/api/auth/ws-ticket')
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>).cookie).toBe('hermes_session_at=at-1')
  })

  it('persists rotated cookies so the NEXT dial and the next launch stay signed in', async () => {
    writeCookieJar(cookieFile, { cookies: { hermes_session_at: 'at-1' }, username: 'sagi' })

    const fetchImpl = vi.fn(async () =>
      jsonResponse({ ticket: 't-1' }, { headers: { 'set-cookie': 'hermes_session_at=at-2; Path=/' } })
    )

    await mintWsTicket({ ...remote, cookieFile }, fetchImpl as unknown as typeof fetch)

    const jar = readCookieJar(cookieFile)
    expect(jar.cookies.hermes_session_at).toBe('at-2')
    // Rotation must not drop the rest of the jar.
    expect(jar.username).toBe('sagi')
    expect(statSync(cookieFile).mode & 0o777).toBe(0o600)
  })

  it('raises an actionable expiry error on 401, naming the command to run', async () => {
    writeCookieJar(cookieFile, { cookies: { hermes_session_at: 'stale' } })
    const fetchImpl = vi.fn(async () => jsonResponse({ detail: 'Unauthorized' }, { status: 401 }))

    await expect(mintWsTicket({ ...remote, cookieFile }, fetchImpl as unknown as typeof fetch)).rejects.toThrow(
      RemoteAuthExpiredError
    )
    await expect(mintWsTicket({ ...remote, cookieFile }, fetchImpl as unknown as typeof fetch)).rejects.toThrow(
      'hermes --connect mini --login'
    )
  })

  it('short-circuits an empty jar without a pointless network round trip', async () => {
    writeCookieJar(cookieFile, { cookies: {} })
    const fetchImpl = vi.fn()

    await expect(mintWsTicket({ ...remote, cookieFile }, fetchImpl as unknown as typeof fetch)).rejects.toThrow(
      RemoteAuthExpiredError
    )
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('treats a 200 without a ticket as a failure rather than dialing with undefined', async () => {
    writeCookieJar(cookieFile, { cookies: { hermes_session_at: 'at-1' } })
    const fetchImpl = vi.fn(async () => jsonResponse({ ttl_seconds: 30 }))

    await expect(mintWsTicket({ ...remote, cookieFile }, fetchImpl as unknown as typeof fetch)).rejects.toThrow(
      'carried no ticket'
    )
  })
})

describe('ticketSubprotocols', () => {
  it('pairs the stable selector with the credential-bearing protocol', () => {
    // The server requires BOTH, and exactly one ticket protocol
    // (hermes_cli/web_server_chat.py:206-217).
    expect(ticketSubprotocols('t-1')).toEqual(['hermes-gateway-v1', 'hermes-gateway-ticket.t-1'])
  })
})
