import { describe, expect, it } from 'vitest'

import { type ChatMessage, chatMessageText, textPart } from '@/lib/chat-messages'

import { withPeerUserPrompt } from './peer-user-prompt'

const msg = (id: string, role: ChatMessage['role'], text: string, rowId?: number): ChatMessage => ({
  id,
  role,
  parts: [textPart(text)],
  ...(rowId === undefined ? {} : { rowId })
})

const settled = [msg('u1', 'user', 'earlier', 10), msg('a1', 'assistant', 'earlier reply', 11)]

describe('withPeerUserPrompt', () => {
  it('a peer whose transcript ends in the previous reply appends the sent text once', () => {
    const next = withPeerUserPrompt(settled, null, 'sent from the iPad', 12, 1_700_000_000)

    expect(next.map(m => [m.role, chatMessageText(m), m.rowId])).toEqual([
      ['user', 'earlier', 10],
      ['assistant', 'earlier reply', 11],
      ['user', 'sent from the iPad', 12]
    ])
    expect(withPeerUserPrompt(next, null, 'sent from the iPad', 12, 1_700_000_001)).toBe(next)
  })

  it('the sender keeps its optimistic bubble whether the event beats the submit reply or not', () => {
    // The wire text carries composer refs the bubble does not show, so text is never the key.
    const beforeReply = [...settled, msg('user-opt', 'user', 'look at this')]
    const afterReply = [...settled, msg('user-opt', 'user', 'look at this', 12)]

    expect(withPeerUserPrompt(beforeReply, null, '@file:a.ts\n\nlook at this', 12, 1)).toBe(beforeReply)
    expect(withPeerUserPrompt(afterReply, null, '@file:a.ts\n\nlook at this', 12, 1)).toBe(afterReply)
  })

  it('a peer whose own last send is bound to another row still appends', () => {
    const own = [...settled, msg('user-own', 'user', 'mine', 12)]

    expect(withPeerUserPrompt(own, null, 'theirs', 13, 1).at(-1)).toMatchObject({ role: 'user', rowId: 13 })
  })
})
