// jc-triage — minimal Claude Code plugin exposing /classify over HTTP.
//
// Spec:
//   POST /classify { "message": "..." }  →  { "text": "<single-line-json>" }
//   GET  /healthz                         →  { "ok": true }
//
// The plugin emits a channel notification to its host Claude session and
// captures the reply tool's output; the gateway's claude-channel backend
// extracts the JSON line from that text.

const PORT = Number(process.env.TRIAGE_PORT ?? 9876);
const TIMEOUT_MS = Number(process.env.TRIAGE_TIMEOUT_MS ?? 15000);

type PendingResolver = (text: string) => void;
const pending = new Map<string, PendingResolver>();

function newId(): string {
  return Math.random().toString(36).slice(2);
}

// --- channel-tool integration --------------------------------------------
// The plugin host runtime injects two helpers when loaded as a Claude plugin:
//   notifyHost(message: string) — emit a notification to the host session
//   onReply(handler: (id: string, text: string) => void) — observe reply tool
// Outside the plugin host (e.g. running the file directly with bun), we use
// stdin/stdout fallbacks for development.

declare const notifyHost: undefined | ((payload: object) => Promise<void>);
declare const onReply: undefined | ((cb: (id: string, text: string) => void) => void);

function emit(payload: object): Promise<void> {
  if (typeof notifyHost === "function") {
    return notifyHost(payload);
  }
  return new Promise((resolve) => {
    process.stdout.write(`[notify] ${JSON.stringify(payload)}\n`);
    resolve();
  });
}

if (typeof onReply === "function") {
  onReply((id, text) => {
    const r = pending.get(id);
    if (r) {
      pending.delete(id);
      r(text);
    }
  });
}

async function classify(message: string): Promise<string> {
  const id = newId();
  const promise = new Promise<string>((resolve, reject) => {
    pending.set(id, resolve);
    setTimeout(() => {
      if (pending.delete(id)) {
        reject(new Error("triage host timeout"));
      }
    }, TIMEOUT_MS);
  });

  await emit({
    type: "triage.classify",
    id,
    instruction:
      "Classify the message below. Respond by calling the `reply` tool with " +
      "exactly one JSON object on a single line: " +
      "{\"class\":\"<class>\",\"brain\":\"<brain>\",\"confidence\":<0..1>}.",
    message,
  });

  return promise;
}

const server = Bun.serve({
  port: PORT,
  hostname: "127.0.0.1",
  async fetch(req) {
    const url = new URL(req.url);
    if (url.pathname === "/healthz") {
      return Response.json({ ok: true });
    }
    if (url.pathname === "/classify" && req.method === "POST") {
      try {
        const body = await req.json();
        const message = String(body?.message ?? "").trim();
        if (!message) {
          return Response.json({ error: "missing message" }, { status: 400 });
        }
        const text = await classify(message);
        return Response.json({ text });
      } catch (err) {
        return Response.json(
          { error: String((err as Error)?.message ?? err) },
          { status: 504 }
        );
      }
    }
    return Response.json({ error: "not found" }, { status: 404 });
  },
});

console.log(`jc-triage listening on http://127.0.0.1:${server.port}`);
