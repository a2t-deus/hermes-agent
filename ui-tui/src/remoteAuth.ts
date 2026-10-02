// Credential half of remote-attach mode (`hermes --connect <name>`).
//
// The Python launcher (`hermes_cli/remote_attach.py`) logs in once and leaves a 0600 cookie
// jar on disk; this module turns that jar into the one credential a WebSocket upgrade can
// actually carry. Browsers — and undici — cannot set `Authorization` on an upgrade, and the
// serve rejects `?token=` outright in gated mode, so the accepted shape is a 30s single-use
// ticket minted over HTTP and passed as a subprotocol
// (`hermes_cli/web_server_chat.py:206-291`).
//
// Two rules follow from that TTL, and both are load-bearing:
//   1. Mint per dial. A ticket is consumed on use and dead after 30s, so every dial —
//      first connect and every reconnect — mints its own. Caching one guarantees a failed
//      reconnect after any backoff longer than the TTL.
//   2. Persist rotation. The serve re-sets the session cookies whenever it refreshes an
//      expired access token (`dashboard_auth/middleware.py:186-196`). Dropping that Set-Cookie
//      means the jar goes stale and the next launch re-prompts for a password.
//
// The ticket never enters the URL (it would land in logs and proxy history); it rides in
// `Sec-WebSocket-Protocol` alongside the stable `hermes-gateway-v1` selector.

import { chmodSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'

export const GATEWAY_WS_PROTOCOL = 'hermes-gateway-v1'
export const GATEWAY_WS_TICKET_PREFIX = 'hermes-gateway-ticket.'
/**
 * Process exit code for "the remote session is dead, sign in again". Distinct from 42
 * (update-relaunch) so the launcher can tell an actionable auth failure from a crash, and
 * non-zero so scripts can branch on it.
 */
export const REMOTE_AUTH_EXIT_CODE = 41
const COOKIE_FILE_MODE = 0o600
const MINT_TIMEOUT_MS = 15_000

export interface RemoteAttachConfig {
  baseUrl: string
  cookieFile: string
  name: string
}

interface CookieJar {
  cookies: Record<string, string>
  url?: string
  username?: string
  version?: number
}

/** A dead cookie set: the user has to re-authenticate, so reconnecting is pointless. */
export class RemoteAuthExpiredError extends Error {
  readonly remoteName: string

  constructor(remoteName: string) {
    super(
      `Remote session expired for "${remoteName}". Run \`hermes --connect ${remoteName} --login\` to sign in again.`
    )
    this.name = 'RemoteAuthExpiredError'
    this.remoteName = remoteName
  }
}

/**
 * Read the three env vars the launcher sets for attach mode, or null when this is an ordinary
 * local launch. Cookie *values* are deliberately not carried in the environment — only the
 * path — so nothing secret is visible to a sibling process reading our argv, and so rotation
 * has somewhere durable to land.
 */
export const resolveRemoteAttach = (env: NodeJS.ProcessEnv = process.env): RemoteAttachConfig | null => {
  const baseUrl = env.HERMES_TUI_REMOTE_URL?.trim()
  const cookieFile = env.HERMES_TUI_REMOTE_COOKIE_FILE?.trim()

  if (!baseUrl || !cookieFile) {
    return null
  }

  const normalized = baseUrl.replace(/\/+$/, '')

  return {
    baseUrl: normalized,
    cookieFile,
    name: env.HERMES_TUI_REMOTE_NAME?.trim() || normalized
  }
}

/** `http://host:port` -> `ws://host:port/api/ws` (and https -> wss). */
export const remoteGatewayWsUrl = (baseUrl: string): string =>
  `${baseUrl.replace(/\/+$/, '').replace(/^http/, 'ws')}/api/ws`

export const readCookieJar = (path: string): CookieJar => {
  try {
    const parsed = JSON.parse(readFileSync(path, 'utf8')) as unknown

    if (!parsed || typeof parsed !== 'object') {
      return { cookies: {} }
    }

    const jar = parsed as CookieJar
    const cookies = jar.cookies && typeof jar.cookies === 'object' ? jar.cookies : {}

    return { ...jar, cookies }
  } catch {
    // Missing or corrupt reads as "no session" — the mint then 401s with an actionable message
    // instead of throwing a JSON parse error at the user.
    return { cookies: {} }
  }
}

/**
 * Write the jar back 0600 via a same-directory temp file, so a crash mid-write cannot leave a
 * truncated jar behind and the secret is never briefly world-readable.
 */
export const writeCookieJar = (path: string, jar: CookieJar): void => {
  const tmp = join(dirname(path), `.cookies-${process.pid}-${Date.now()}.tmp`)

  try {
    writeFileSync(tmp, `${JSON.stringify({ ...jar, version: jar.version ?? 1 }, null, 2)}\n`, {
      encoding: 'utf8',
      mode: COOKIE_FILE_MODE
    })
    chmodSync(tmp, COOKIE_FILE_MODE)
    renameSync(tmp, path)
  } catch {
    try {
      unlinkSync(tmp)
    } catch {
      // best effort
    }
  }
}

export const cookieHeader = (jar: CookieJar): string =>
  Object.entries(jar.cookies)
    .map(([name, value]) => `${name}=${value}`)
    .join('; ')

/**
 * Merge `Set-Cookie` response headers into the jar. Only the name=value pair is kept: this
 * client speaks to exactly one origin, so Domain/Path/SameSite attributes carry no information
 * we could act on, while `Max-Age=0` still has to be honoured as a deletion.
 */
export const applySetCookie = (jar: CookieJar, headers: string[]): CookieJar => {
  const cookies = { ...jar.cookies }

  for (const raw of headers) {
    const [pair, ...attrs] = raw.split(';')
    const eq = pair.indexOf('=')

    if (eq <= 0) {
      continue
    }

    const name = pair.slice(0, eq).trim()
    const value = pair.slice(eq + 1).trim()
    const expired = attrs.some(a => /^\s*max-age\s*=\s*0\s*$/i.test(a))

    if (!name) {
      continue
    }

    if (expired) {
      delete cookies[name]
    } else {
      cookies[name] = value
    }
  }

  return { ...jar, cookies }
}

/** Node splits (or folds) multiple Set-Cookie headers differently across runtimes. */
const setCookieHeaders = (headers: Headers): string[] => {
  const getSetCookie = (headers as unknown as { getSetCookie?: () => string[] }).getSetCookie

  if (typeof getSetCookie === 'function') {
    return getSetCookie.call(headers)
  }

  const single = headers.get('set-cookie')

  return single ? [single] : []
}

/**
 * Mint one single-use WS ticket for the next dial, persisting any rotated cookies.
 *
 * Throws {@link RemoteAuthExpiredError} on 401 so the caller can stop reconnecting: retrying a
 * dead cookie set just burns backoff cycles and never recovers.
 */
export const mintWsTicket = async (remote: RemoteAttachConfig, fetchImpl: typeof fetch = fetch): Promise<string> => {
  const jar = readCookieJar(remote.cookieFile)
  const cookie = cookieHeader(jar)

  if (!cookie) {
    throw new RemoteAuthExpiredError(remote.name)
  }

  const response = await fetchImpl(`${remote.baseUrl}/api/auth/ws-ticket`, {
    body: '',
    headers: { cookie },
    method: 'POST',
    redirect: 'manual',
    signal: AbortSignal.timeout(MINT_TIMEOUT_MS)
  })

  const rotated = setCookieHeaders(response.headers)

  if (rotated.length > 0) {
    writeCookieJar(remote.cookieFile, applySetCookie(jar, rotated))
  }

  if (response.status === 401 || response.status === 403) {
    throw new RemoteAuthExpiredError(remote.name)
  }

  if (!response.ok) {
    throw new Error(`ws-ticket mint failed (HTTP ${response.status})`)
  }

  const payload = (await response.json()) as { ticket?: unknown }

  if (typeof payload.ticket !== 'string' || !payload.ticket) {
    throw new Error('ws-ticket response carried no ticket')
  }

  return payload.ticket
}

/** Subprotocol list for an authenticated gateway upgrade. */
export const ticketSubprotocols = (ticket: string): string[] => [
  GATEWAY_WS_PROTOCOL,
  `${GATEWAY_WS_TICKET_PREFIX}${ticket}`
]
