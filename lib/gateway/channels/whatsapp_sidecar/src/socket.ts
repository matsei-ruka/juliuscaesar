/**
 * Baileys WhatsApp Web socket lifecycle.
 *
 * Creates and manages a Baileys socket connection. Handles:
 *   - Socket creation with persisted auth
 *   - Connection state tracking
 *   - Event forwarding to the protocol layer
 *   - Graceful shutdown
 */

import makeWASocket, {
  DisconnectReason,
  fetchLatestBaileysVersion,
  useMultiFileAuthState,
  type WASocket,
  type ConnectionState,
} from "@whiskeysockets/baileys";
import { Boom } from "@hapi/boom";
import { buildAuthState } from "./auth.js";
import { normalizeMessage } from "./normalize.js";
import { sendMessage } from "./send.js";
import type { OutboundMessage, OutgoingEvent } from "./protocol.js";

export interface SocketCallbacks {
  /** Emit a JSON line to stdout for the Python channel. */
  emit: (obj: OutgoingEvent) => void;
  /** Log a message to stderr (not the protocol stream). */
  log: (msg: string) => void;
  /** Called when the socket reaches the 'open' state. */
  onOpen: (selfJid: string) => void;
}

let sock: WASocket | null = null;
let stopping = false;

export async function startSocket(
  authDir: string,
  callbacks: SocketCallbacks
): Promise<void> {
  stopping = false;

  const { state, save } = buildAuthState(authDir);
  const { version, isLatest } = await fetchLatestBaileysVersion();

  callbacks.log(
    `baileys version: ${version.join(".")} (latest: ${isLatest})`
  );

  sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false,
    logger: {
      trace: () => {},
      debug: () => {},
      info: (_: any, msg: string) => callbacks.log(`[baileys] ${msg}`),
      warn: (_: any, msg: string) => callbacks.log(`[baileys:warn] ${msg}`),
      error: (_: any, msg: string) => callbacks.log(`[baileys:error] ${msg}`),
      child: () => ({
        trace() {},
        debug() {},
        info(_: any, msg: string) {
          callbacks.log(`[baileys:child] ${msg}`);
        },
        warn(_: any, msg: string) {
          callbacks.log(`[baileys:child:warn] ${msg}`);
        },
      error(_: any, msg: string) {
          callbacks.log(`[baileys:child:error] ${msg}`);
        },
        child() {
          return this;
        },
        level: "info",
      }),
      level: "info",
    },
    emitOwnEvents: false,
    markOnlineOnConnect: true,
    shouldSyncHistoryMessage: () => false, // don't replay history
    qrTimeout: 60_000,
  });

  // -- connection updates --
  sock.ev.on("connection.update", (update: Partial<ConnectionState>) => {
    const { connection, lastDisconnect, qr, isNewLogin } = update;

    if (qr) {
      callbacks.emit({
        type: "qr",
        qr,
      });
    }

    if (connection === "open") {
      const selfJid = sock!.user?.id?.split(":")[0] ?? "unknown";
      callbacks.emit({
        type: "connection",
        state: "open",
        self_jid: selfJid,
      });
      callbacks.onOpen(selfJid);
      return;
    }

    if (connection === "close") {
      const statusCode = (lastDisconnect?.error as Boom)?.output?.statusCode;
      const reason =
        (lastDisconnect?.error as any)?.message ?? "unknown";

      const shouldReconnect =
        statusCode !== DisconnectReason.loggedOut &&
        !stopping;

      callbacks.emit({
        type: "connection",
        state: "close",
        reason,
        status_code: statusCode,
        will_reconnect: shouldReconnect,
      });

      if (!shouldReconnect) {
        callbacks.emit({
          type: "connection",
          state: statusCode === DisconnectReason.loggedOut
            ? "logged_out"
            : "auth_missing",
          reason,
        });
      }

      // Attempt reconnect unless stopped or logged out.
      if (shouldReconnect) {
        callbacks.log(`reconnecting (reason: ${reason})…`);
        setTimeout(() => {
          if (!stopping) {
            startSocket(authDir, callbacks).catch((err) =>
              callbacks.log(`reconnect failed: ${err}`)
            );
          }
        }, 3000);
      }
    }

    if (connection === "connecting") {
      callbacks.emit({
        type: "connection",
        state: "connecting",
      });
    }
  });

  // -- credentials saved --
  sock.ev.on("creds.update", () => {
    save().catch((err) => callbacks.log(`creds save failed: ${err}`));
  });

  // -- inbound messages --
  sock.ev.on("messages.upsert", ({ messages, type }) => {
    if (type !== "notify") return; // skip history sync messages

    for (const msg of messages) {
      // Skip messages from self
      if (msg.key.fromMe) continue;

      try {
        const normalized = normalizeMessage(msg, sock!.user?.id ?? "");
        if (normalized) {
          callbacks.emit(normalized);
        }
      } catch (err) {
        callbacks.log(`normalize failed: ${err}`);
      }
    }
  });
}

/** Send an outbound message through the socket. */
export async function sendOne(
  msg: OutboundMessage
): Promise<{ ok: boolean; message_id?: string; error?: string }> {
  if (!sock) {
    return { ok: false, error: "socket not connected" };
  }
  return sendMessage(sock, msg);
}

/** Stop the socket and clean up. */
export async function stopSocket(): Promise<void> {
  stopping = true;
  if (sock) {
    // Baileys socket doesn't expose a direct close method on the WASocket type.
    // The connection is closed by ending the WS internally or via ev listeners.
    // We signal stop and let the next connection.close event skip reconnect.
    sock = null;
  }
}
