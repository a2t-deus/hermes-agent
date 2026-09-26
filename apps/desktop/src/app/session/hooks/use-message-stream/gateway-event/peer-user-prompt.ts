import { type ChatMessage, textPart } from '@/lib/chat-messages'

import { finalizeInterruptedMessages } from '../../use-prompt-actions/rewind'

/**
 * `user.prompt` reaches every viewer of the session, the sender included.
 * The sender already drew an optimistic bubble: it is the transcript's last
 * message, a user row not yet bound to a different durable row (the
 * prompt.submit reply may stamp `rowId` before or after this event). A peer's
 * transcript ends in the previous reply instead, so it appends the text.
 * `prompt.submit` params are closed (`extra="forbid"`), so a client nonce would
 * break sends to older backends — the durable `row_id` is the only shared key.
 */
export function withPeerUserPrompt(
  messages: ChatMessage[],
  streamId: null | string,
  text: string,
  rowId: number | undefined,
  occurredAt: number
): ChatMessage[] {
  if (rowId !== undefined && messages.some(m => m.rowId === rowId)) {
    return messages
  }

  const last = messages.at(-1)

  if (last?.role === 'user' && (last.rowId === undefined || last.rowId === rowId)) {
    return messages
  }

  return [
    ...finalizeInterruptedMessages(messages, streamId, occurredAt),
    {
      id: `peer-user-${rowId ?? Math.round(occurredAt * 1000)}`,
      role: 'user',
      parts: [textPart(text)],
      timestamp: occurredAt,
      ...(rowId === undefined ? {} : { rowId })
    }
  ]
}
