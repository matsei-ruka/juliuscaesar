/**
 * JSON protocol types shared between the sidecar and Python channel.
 *
 * The sidecar communicates over stdio: one JSON object per line on stdout
 * (sidecar → Python) and stdin (Python → sidecar).
 */

// ---- Sidecar → Python (stdout) ----

export interface QrEvent {
  type: "qr";
  qr: string;
}

export interface ConnectionEvent {
  type: "connection";
  state: "open" | "close" | "connecting" | "reconnecting" | "logged_out" | "auth_missing";
  self_jid?: string;
  reason?: string;
  status_code?: number;
  will_reconnect?: boolean;
}

export interface MediaInfo {
  type: "image" | "audio" | "video" | "document" | "sticker";
  mime_type: string;
  height?: number;
  width?: number;
  seconds?: number;
  file_name?: string;
  file_size?: number;
}

export interface NormalizedMessage {
  type: "message";
  message_id: string;
  remote_jid: string;
  sender_jid: string;
  chat_type: "dm" | "group";
  from_me: boolean;
  push_name?: string;
  timestamp: string;
  text: string | null;
  mentions: string[];
  quoted_message_id: string | null;
  media: MediaInfo | null;
  raw_kind: "conversation" | "extended_text" | "media";
  /** Present only for group messages. */
  group_jid?: string;
}

export interface SendResultEvent {
  type: "send_result";
  id: string;
  ok: boolean;
  message_id?: string;
  error?: string;
}

export interface ErrorEvent {
  type: "error";
  fatal: boolean;
  reason: string;
}

export type OutgoingEvent =
  | QrEvent
  | ConnectionEvent
  | NormalizedMessage
  | SendResultEvent
  | DownloadResultEvent
  | ErrorEvent;

// ---- Python → Sidecar (stdin) ----

export interface SendCommand {
  type: "send";
  id: string;
  to: string;
  text: string;
  quoted_message_id?: string | null;
  media?: null;
}

export interface DownloadCommand {
  type: "download";
  id: string;
  message_key: {
    id: string;
    remoteJid: string;
    fromMe: boolean;
  };
  dest_path: string;
}

export interface StopCommand {
  type: "stop";
}

export type IncomingCommand = SendCommand | DownloadCommand | StopCommand;

// ---- Command results ----

export interface DownloadResultEvent {
  type: "download_result";
  id: string;
  ok: boolean;
  dest_path?: string;
  mime_type?: string;
  file_size?: number;
  error?: string;
}

// ---- Message used by send.ts internally ----

export interface OutboundMessage {
  to: string;
  text: string;
  quoted_message_id?: string | null;
}
