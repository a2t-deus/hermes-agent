/**
 * Two windows are two WebSocket clients of one local `hermes serve` showing
 * the same chat. The peer window sends; the source window must show that user
 * message before any assistant output of the turn (the completion is held
 * before its first token), and neither window may render it twice.
 *
 * Prerequisite: `npm run build` so dist/ exists.
 */
import { MOCK_REPLY } from '../../../tests-js/scripts/mock-server'

import { type MockBackendFixture, setupMockBackend, waitForAppReady } from './fixtures'
import { expect, installErrorBannerGuard, type Page, test } from './test'

const PEER_TEXT = 'E2E_PEER_ECHO sent from the other device'

const composer = (page: Page) =>
  page.locator('[data-slot="composer-root"] [contenteditable="true"]').filter({ visible: true }).first()

const replies = (page: Page) =>
  page.locator('[data-slot="aui_assistant-message-content"]').getByText(MOCK_REPLY, { exact: true })

const peerBubbles = (page: Page) => page.getByText(PEER_TEXT, { exact: true })

let fixture: MockBackendFixture | null = null

test.beforeAll(async () => {
  fixture = await setupMockBackend({ mockServer: { holdFirstCompletionContaining: 'E2E_PEER_ECHO' } })
  await waitForAppReady(fixture, 120_000)
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

test.setTimeout(240_000)

test('a message sent from a peer window appears in the source before the reply, once in each', async () => {
  const { app, mock, page: source } = fixture!

  await composer(source).fill('Start the shared chat.')
  await composer(source).press('Enter')
  await expect(replies(source)).toHaveCount(1, { timeout: 60_000 })

  const opened = app.waitForEvent('window')
  await source.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+N' : 'Control+Shift+N')
  const peer = await opened
  installErrorBannerGuard(peer)
  await expect(replies(peer)).toHaveCount(1, { timeout: 90_000 })

  await composer(peer).fill(PEER_TEXT)
  await composer(peer).press('Enter')
  await mock.waitForHeldCompletion()

  // The model has produced nothing for this turn yet: the text can only have
  // come from the live user.prompt event, not from the reply or a reload.
  await expect(peerBubbles(source)).toHaveCount(1, { timeout: 30_000 })
  await expect(replies(source)).toHaveCount(1)
  await expect(peerBubbles(peer)).toHaveCount(1)

  mock.releaseHeldStream()
  await expect(replies(source)).toHaveCount(2, { timeout: 60_000 })
  await expect(replies(peer)).toHaveCount(2, { timeout: 60_000 })
  await expect(peerBubbles(source)).toHaveCount(1)
  await expect(peerBubbles(peer)).toHaveCount(1)
})
