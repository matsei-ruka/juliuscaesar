/**
 * Outbound message sending via Baileys.
 *
 * Sends text messages with optional quoted reply. Media sending is deferred
 * to a later milestone (Phase 5).
 */

import type { WASocket } from "@whiskeysockets/baileys";
import { downloadMediaMessage } from "@whiskeysockets/baileys";
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

/**
 * Download media from a WhatsApp message.
 * Returns path to the downloaded file.
 */
export async function downloadMedia(
  sock: WASocket,
  msgKey: { id: string; remoteJid: string; fromMe: boolean },
  destPath: string
): Promise<{ ok: boolean; dest_path?: string; mime_type?: string; file_size?: number; error?: string }> {
  try {
    // Build a minimal message object for downloadMediaMessage
    const msg = {
      key: {
        id: msgKey.id,
        remoteJid: msgKey.remoteJid,
        fromMe: msgKey.fromMe,
      },
    } as any;

    const buffer = await downloadMediaMessage(msg, "buffer", {});
    if (!buffer || buffer.length === 0) {
      return { ok: false, error: "empty or missing media" };
    }

    const buf = buffer;
    const { writeFileSync, mkdirSync } = await import("node:fs");
    const { dirname } = await import("node:path");
    mkdirSync(dirname(destPath), { recursive: true });
    writeFileSync(destPath, buf);

    // Detect MIME type from buffer header
    let mimeType = "application/octet-stream";
    if (buf[0] === 0xff && buf[1] === 0xd8) mimeType = "image/jpeg";
    else if (buf[0] === 0x89 && buf[1] === 0x50) mimeType = "image/png";
    else if (buf[0] === 0x47 && buf[1] === 0x49) mimeType = "image/gif";
    else if (buf[0] === 0x52 && buf[1] === 0x49) mimeType = "image/webp";
    else if (buf[0] === 0x4f && buf[1] === 0x67) mimeType = "audio/ogg";
    else if (buf[0] === 0x00 && buf[1] === 0x00) mimeType = "video/mp4";

    return {
      ok: true,
      dest_path: destPath,
      mime_type: mimeType,
      file_size: buf.length,
    };
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return { ok: false, error: message };
  }
}
