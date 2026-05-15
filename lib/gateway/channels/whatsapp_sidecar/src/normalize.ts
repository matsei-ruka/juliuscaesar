/**
 * Normalize a Baileys WAMessage into the JC sidecar JSON schema.
 *
 * The sidecar must not know about JuliusCaesar policy — this module only
 * extracts the shape. Policy decisions (Trusted/External/Blocked, group
 * mention gate) happen in the Python channel.
 */

import type { WAMessage } from "@whiskeysockets/baileys";
import { jidNormalizedUser, isJidBroadcast, isJidStatusBroadcast } from "@whiskeysockets/baileys";
import type { NormalizedMessage } from "./protocol.js";

/**
 * Extract the best available text from a message.
 * Handles: conversation (plain), extendedTextMessage (rich), button replies,
 * and interactive message text.
 */
function extractText(msg: WAMessage): string | null {
  const content = msg.message;
  if (!content) return null;

  if (content.conversation) return content.conversation;
  if (content.extendedTextMessage?.text) return content.extendedTextMessage.text;
  if (content.imageMessage?.caption) return content.imageMessage.caption;
  if (content.videoMessage?.caption) return content.videoMessage.caption;
  if (content.documentMessage?.caption) return content.documentMessage.caption;
  if (content.buttonsResponseMessage?.selectedButtonId)
    return content.buttonsResponseMessage.selectedButtonId;
  if (content.listResponseMessage?.singleSelectReply?.selectedRowId)
    return content.listResponseMessage.singleSelectReply.selectedRowId;
  if (content.templateButtonReplyMessage?.selectedId)
    return content.templateButtonReplyMessage.selectedId;

  return null;
}

/**
 * Determine the chat type: "dm" or "group".
 */
function chatType(remoteJid: string): "dm" | "group" {
  return remoteJid.endsWith("@g.us") ? "group" : "dm";
}

/**
 * Extract mentioned JIDs from extendedTextMessage.contextInfo.
 */
function extractMentions(msg: WAMessage): string[] {
  const mentioned =
    msg.message?.extendedTextMessage?.contextInfo?.mentionedJid;
  if (!mentioned || !Array.isArray(mentioned)) return [];
  return mentioned.filter(
    (jid): jid is string => typeof jid === "string"
  );
}

/**
 * Extract quoted message ID from contextInfo.
 */
function extractQuotedId(msg: WAMessage): string | null {
  const stanzaId =
    msg.message?.extendedTextMessage?.contextInfo?.stanzaId;
  return typeof stanzaId === "string" && stanzaId ? stanzaId : null;
}

/**
 * Detect media presence in the message.
 */
function extractMedia(msg: WAMessage): NormalizedMessage["media"] {
  const content = msg.message;
  if (!content) return null;

  if (content.imageMessage) {
    return {
      type: "image",
      mime_type: content.imageMessage.mimetype ?? "image/jpeg",
      height: content.imageMessage.height ?? 0,
      width: content.imageMessage.width ?? 0,
      file_size: (content.imageMessage.fileLength as number) ?? 0,
    };
  }
  if (content.audioMessage) {
    return {
      type: "audio",
      mime_type: content.audioMessage.mimetype ?? "audio/ogg",
      seconds: content.audioMessage.seconds ?? 0,
      file_size: (content.audioMessage.fileLength as number) ?? 0,
    };
  }
  if (content.videoMessage) {
    return {
      type: "video",
      mime_type: content.videoMessage.mimetype ?? "video/mp4",
      seconds: content.videoMessage.seconds ?? 0,
      file_size: (content.videoMessage.fileLength as number) ?? 0,
    };
  }
  if (content.documentMessage) {
    return {
      type: "document",
      mime_type: content.documentMessage.mimetype ?? "application/octet-stream",
      file_name: content.documentMessage.fileName ?? undefined,
      file_size: (content.documentMessage.fileLength as number) ?? 0,
    };
  }
  if (content.stickerMessage) {
    return {
      type: "sticker",
      mime_type: content.stickerMessage.mimetype ?? "image/webp",
    };
  }

  return null;
}

/**
 * Convert a Baileys WAMessage to a normalized JSON object for the Python channel.
 * Returns null if the message should be skipped (protocol messages, broadcasts, etc.).
 */
export function normalizeMessage(
  msg: WAMessage,
  selfJid: string
): NormalizedMessage | null {
  // Skip messages from self
  if (msg.key.fromMe) return null;

  const remoteJid = msg.key.remoteJid;
  if (!remoteJid) return null;

  // Skip broadcast and status messages
  if (isJidBroadcast(remoteJid) || isJidStatusBroadcast(remoteJid)) return null;

  // Skip protocol messages (no content, no text, no media)
  const text = extractText(msg);
  const media = extractMedia(msg);
  if (!text && !media) return null;

  const senderJid = msg.key.participant ?? jidNormalizedUser(remoteJid);
  const type = chatType(remoteJid);
  const timestamp = msg.messageTimestamp
    ? new Date((msg.messageTimestamp as number) * 1000).toISOString()
    : new Date().toISOString();

  const normalized: NormalizedMessage = {
    type: "message",
    message_id: msg.key.id ?? "",
    remote_jid: remoteJid,
    sender_jid: senderJid,
    chat_type: type,
    from_me: false,
    push_name: msg.pushName ?? undefined,
    timestamp,
    text: text ?? null,
    mentions: extractMentions(msg),
    quoted_message_id: extractQuotedId(msg),
    media: media ?? null,
    raw_kind:
      msg.message?.conversation
        ? "conversation"
        : msg.message?.extendedTextMessage
          ? "extended_text"
          : "media",
    // If the message is from a group and sender differs from remote_jid,
    // the sender is a participant. Include group_jid for routing.
    ...(type === "group"
      ? { group_jid: remoteJid }
      : {}),
  };

  return normalized;
}
