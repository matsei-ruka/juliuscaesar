/**
 * Outbound message sending via Baileys.
 *
 * Sends text messages with optional quoted reply. Media sending is deferred
 * to a later milestone (Phase 5).
 */

import type { WASocket } from "@whiskeysockets/baileys";
import { jidNormalizedUser } from "@whiskeysockets/baileys";
import type { OutboundMessage, SendResultEvent } from "./protocol.js";

/** Chunk a long message into WhatsApp-safe segments. */
function chunkText(text: string, limit: number = 3500): string[] {
  if (text.length <= limit) return [text];

  const chunks: string[] = [];
  let remaining = text;

  while (remaining.length > 0) {
    if (remaining.length <= limit) {
      chunks.push(remaining);
      break;
    }
    // Try to break at a paragraph or sentence boundary.
    let cut = remaining.lastIndexOf("\n\n", limit);
    if (cut === -1 || cut < limit / 2) {
      cut = remaining.lastIndexOf("\n", limit);
    }
    if (cut === -1 || cut < limit / 2) {
      cut = remaining.lastIndexOf(". ", limit);
    }
    if (cut === -1 || cut < limit / 2) {
      cut = remaining.lastIndexOf(" ", limit);
    }
    if (cut === -1 || cut < limit / 2) {
      cut = limit;
    }
    chunks.push(remaining.slice(0, cut).trim());
    remaining = remaining.slice(cut).trim();
  }

  return chunks;
}

/**
 * Send a text message (possibly chunked) via Baileys.
 * Returns a SendResultEvent-compatible result.
 */
export async function sendMessage(
  sock: WASocket,
  msg: OutboundMessage
): Promise<{ ok: boolean; message_id?: string; error?: string }> {
  const jid = jidNormalizedUser(msg.to);
  const chunks = chunkText(msg.text);

  const quotedOptions = msg.quoted_message_id
    ? { quoted: { key: { id: msg.quoted_message_id, remoteJid: jid }, message: {} } }
    : {};

  let lastId: string | undefined;

  try {
    for (const chunk of chunks) {
      const result = await sock.sendMessage(jid, { text: chunk }, quotedOptions as any);
      // result is proto.WebMessageInfo or void. Extract message id.
      if (result && typeof result === "object" && "key" in result) {
        lastId = (result.key as any)?.id;
      }
    }
    return { ok: true, message_id: lastId };
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return { ok: false, error: message };
  }
}
