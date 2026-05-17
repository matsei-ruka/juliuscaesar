/**
 * Auth state persistence for the JC WhatsApp sidecar.
 *
 * Baileys manages auth state as an object with two parts:
 *   - `creds`: AuthenticationCreds (keys, identity, me, etc.)
 *   - `keys`: SignalKeyStore (pre-keys, sessions, sender keys)
 *
 * We persist both to disk so the socket can resume without re-scanning
 * a QR code. The auth directory is provided via the `--auth-dir` CLI arg
 * and defaults to `<instance>/state/channels/whatsapp/auth/<account_id>/`.
 */

import { readFileSync, writeFileSync, renameSync, mkdirSync, existsSync } from "node:fs";
import { join } from "node:path";
import type {
  AuthenticationCreds,
  SignalDataSet,
  SignalDataTypeMap,
  AuthenticationState,
} from "@whiskeysockets/baileys";
import { BufferJSON, initAuthCreds } from "@whiskeysockets/baileys";

const CREDS_FILE = "creds.json";
const KEYS_DIR = "keys";

export interface PersistedAuth {
  creds: AuthenticationCreds;
  keys: Record<string, SignalDataSet>;
}

/** Read persisted auth state from disk. Returns null if not found. */
export function readAuth(authDir: string): PersistedAuth | null {
  const credsPath = join(authDir, CREDS_FILE);
  const keysDir = join(authDir, KEYS_DIR);

  if (!existsSync(credsPath)) return null;

  try {
    const credsRaw = readFileSync(credsPath, "utf-8");
    const creds = JSON.parse(credsRaw, BufferJSON.reviver) as AuthenticationCreds;

    const keys: Record<string, SignalDataSet> = {};
    if (existsSync(keysDir)) {
      const keyTypes: (keyof SignalDataTypeMap)[] = [
        "pre-key",
        "session",
        "sender-key",
        "app-state-sync-key",
        "app-state-sync-version",
        "sender-key-memory",
      ];
      for (const type of keyTypes) {
        const keyPath = join(keysDir, `${type}.json`);
        if (existsSync(keyPath)) {
          const raw = readFileSync(keyPath, "utf-8");
          keys[type] = JSON.parse(raw, BufferJSON.reviver) as SignalDataSet;
        }
      }
    }

    return { creds, keys };
  } catch {
    return null;
  }
}

/** Write auth state to disk atomically (tmp → rename). */
export function writeAuth(authDir: string, state: PersistedAuth): void {
  mkdirSync(authDir, { recursive: true, mode: 0o700 });
  const keysDir = join(authDir, KEYS_DIR);
  mkdirSync(keysDir, { recursive: true, mode: 0o700 });

  // Write creds atomically: write to .tmp, then rename over real file.
  const credsPath = join(authDir, CREDS_FILE);
  const credsTmp = join(authDir, `.${CREDS_FILE}.tmp`);
  writeFileSync(credsTmp, JSON.stringify(state.creds, BufferJSON.replacer, 2), {
    encoding: "utf-8",
    mode: 0o600,
  });
  // Keep a .bak of the previous creds for recovery.
  if (existsSync(credsPath)) {
    const credsBak = join(authDir, `${CREDS_FILE}.bak`);
    writeFileSync(credsBak, readFileSync(credsPath), { mode: 0o600 });
  }
  renameSync(credsTmp, credsPath);

  // Write each key type atomically.
  for (const [type, data] of Object.entries(state.keys)) {
    const keyPath = join(keysDir, `${type}.json`);
    const keyTmp = join(keysDir, `.${type}.json.tmp`);
    writeFileSync(keyTmp, JSON.stringify(data, BufferJSON.replacer, 2), {
      encoding: "utf-8",
      mode: 0o600,
    });
    renameSync(keyTmp, keyPath);
  }
}

/**
 * Build the Baileys AuthenticationState from persisted or fresh creds.
 * This is what `makeWASocket` accepts in its config.
 */
export function buildAuthState(authDir: string): {
  state: AuthenticationState;
  save: () => Promise<void>;
} {
  const persisted = readAuth(authDir);
  const creds: AuthenticationCreds =
    persisted?.creds ?? initAuthCreds();
  const keyStore: Record<string, SignalDataSet> = persisted?.keys ?? {};

  const save = async () => {
    writeAuth(authDir, { creds, keys: keyStore });
  };

  const state: AuthenticationState = {
    creds,
    keys: {
      get<T extends keyof SignalDataTypeMap>(
        type: T,
        ids: readonly string[]
      ): Promise<Record<string, SignalDataTypeMap[T]>> {
        const data: Record<string, any> = {};
        const store: Record<string, SignalDataTypeMap[T]> =
          (keyStore[type] as Record<string, SignalDataTypeMap[T]>) ?? {};
        for (const id of ids) {
          if (id in store) {
            data[id] = store[id];
          }
        }
        return Promise.resolve(data);
      },
      set(data: Record<string, any>): Promise<void> {
        for (const category in data) {
          keyStore[category] = keyStore[category] ?? {};
          Object.assign(keyStore[category], data[category]);
        }
        return save();
      },
    },
  };

  return { state, save };
}
