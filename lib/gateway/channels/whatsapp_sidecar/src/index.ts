/**
 * JC WhatsApp sidecar — main entry point.
 *
 * Communicates with the Python channel over stdio JSON lines:
 *   - stdin:  incoming commands  (send, stop)
 *   - stdout: outgoing events    (qr, connection, message, send_result, error)
 *   - stderr: debug logs only    (never protocol data)
 *
 * Usage:
 *   node dist/index.js --auth-dir <path> [--account-id default]
 *
 * The sidecar starts a Baileys socket. If auth exists, it reconnects.
 * If not, it emits a QR code on stdout for the operator to scan.
 * Once connected, it normalizes inbound messages and emits them.
 */

import { createInterface } from "node:readline";
import { parseArgs } from "node:util";
import { startSocket, stopSocket, sendOne } from "./socket.js";
import type {
  OutgoingEvent,
  IncomingCommand,
  SendCommand,
} from "./protocol.js";

// ---- CLI args ----

const { values: args } = parseArgs({
  options: {
    "auth-dir": { type: "string" },
    "account-id": { type: "string", default: "default" },
  },
});

const authDir = args["auth-dir"];
if (!authDir) {
  process.stderr.write("fatal: --auth-dir is required\n");
  process.exit(1);
}

const accountId = args["account-id"]!;

// ---- Stdio setup ----

const rl = createInterface({
  input: process.stdin,
  output: process.stdout,
  terminal: false,
});

// Buffer for partial lines (safety: stdin is line-delimited JSON)
let buffer = "";

/** Emit a JSON object on stdout. Must be the only thing writing to stdout. */
function emitEvent(event: OutgoingEvent): void {
  const line = JSON.stringify({ ...event, account_id: accountId });
  process.stdout.write(line + "\n");
}

/** Log to stderr only. Never to stdout. */
function log(msg: string): void {
  process.stderr.write(`[jc-whatsapp] ${msg}\n`);
}

// ---- Start socket ----

let selfJid = "";

startSocket(authDir, {
  emit: emitEvent,
  log,
  onOpen: (jid) => {
    selfJid = jid;
    log(`connected as ${jid}`);
  },
}).catch((err) => {
  log(`fatal: socket start failed: ${err}`);
  emitEvent({
    type: "error",
    fatal: true,
    reason: `socket_start: ${err instanceof Error ? err.message : String(err)}`,
  });
  process.exit(1);
});

// ---- Incoming commands (stdin) ----

rl.on("line", (raw) => {
  const line = raw.trim();
  if (!line) return;

  let cmd: IncomingCommand;
  try {
    cmd = JSON.parse(line) as IncomingCommand;
  } catch {
    log(`invalid stdin JSON: ${line}`);
    return;
  }

  switch (cmd.type) {
    case "send": {
      const s = cmd as SendCommand;
      sendOne({
        to: s.to,
        text: s.text,
        quoted_message_id: s.quoted_message_id,
      })
        .then((result) => {
          emitEvent({
          type: "send_result",
          id: s.id,
          ...result,
        });
      })
      .catch((err) => {
        emitEvent({
          type: "send_result",
          id: s.id,
          ok: false,
          error: err instanceof Error ? err.message : String(err),
        });
        });
      break;
    }

    case "stop":
      log("received stop command, shutting down");
      stopSocket().then(() => {
        rl.close();
        process.exit(0);
      });
      break;

    default:
      log(`unknown command type: ${(cmd as any).type}`);
  }
});

rl.on("close", () => {
  log("stdin closed, shutting down");
  stopSocket().then(() => process.exit(0));
});

// ---- Graceful shutdown ----

process.on("SIGTERM", () => {
  log("SIGTERM received");
  stopSocket().then(() => process.exit(0));
});

process.on("SIGINT", () => {
  log("SIGINT received");
  stopSocket().then(() => process.exit(0));
});
