import { apiFetch, apiUrl } from "@/lib/api";
import { invalidateClientCache, withClientCache } from "@/lib/client-cache";
import type {
  ImportSource,
  LocalSessionInfo,
  NormalizedSession,
} from "@/lib/chat-import/types";
import type { SessionSummary } from "@/lib/session-api";

/** Per-session outcome echoed back by the import endpoint. */
export interface ImportSessionOutcome {
  external_id: string;
  session_id?: string;
  imported: boolean;
  reason?: string;
}

export interface ImportResult {
  imported: number;
  skipped: number;
  sessions: ImportSessionOutcome[];
}

const IMPORTED_CACHE_PREFIX = "imported-sessions:";

export async function importChatHistory(
  source: ImportSource,
  sessions: NormalizedSession[],
  agent?: { id: string; name: string },
): Promise<ImportResult> {
  if (sessions.length === 0) {
    throw new Error("No sessions to import");
  }
  const response = await apiFetch(apiUrl("/api/imports/chat-history"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      source,
      sessions,
      agent_id: agent?.id ?? "",
      agent_name: agent?.name ?? "",
    }),
  });
  if (!response.ok) {
    throw new Error(`Import failed: ${response.status}`);
  }
  const data = (await response.json()) as ImportResult;
  invalidateClientCache(IMPORTED_CACHE_PREFIX);
  return data;
}

/**
 * Upload a large static export without crossing the backend's per-request
 * session ceiling. Re-imports are idempotent, so a retry after a partial
 * network failure safely skips batches that already landed.
 */
export async function importChatHistoryInBatches(
  source: ImportSource,
  sessions: NormalizedSession[],
  options?: {
    batchSize?: number;
    onProgress?: (done: number, total: number) => void;
  },
): Promise<ImportResult> {
  if (sessions.length === 0) throw new Error("No sessions to import");
  const batchSize = Math.max(1, Math.min(1000, options?.batchSize ?? 100));
  const combined: ImportResult = { imported: 0, skipped: 0, sessions: [] };
  for (let start = 0; start < sessions.length; start += batchSize) {
    const batch = sessions.slice(start, start + batchSize);
    const result = await importChatHistory(source, batch);
    combined.imported += result.imported;
    combined.skipped += result.skipped;
    combined.sessions.push(...result.sessions);
    options?.onProgress?.(
      Math.min(start + batch.length, sessions.length),
      sessions.length,
    );
  }
  return combined;
}

export async function listImportedSessions(
  limit = 200,
  offset = 0,
  options?: { force?: boolean },
): Promise<SessionSummary[]> {
  const qs = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  return withClientCache<SessionSummary[]>(
    `${IMPORTED_CACHE_PREFIX}${limit}:${offset}`,
    async () => {
      const response = await apiFetch(
        apiUrl(`/api/imports/chat-history?${qs.toString()}`),
        { cache: "no-store" },
      );
      if (!response.ok) {
        throw new Error(`Request failed: ${response.status}`);
      }
      const data = (await response.json()) as { sessions: SessionSummary[] };
      return data.sessions ?? [];
    },
    { force: options?.force, ttlMs: 15_000 },
  );
}

/* ── Local (desktop) source scanning ───────────────────────────────────────
 * On the desktop build the backend runs on the user's machine and can scan
 * agent folders server-side (no File System Access API). These calls stay
 * read-only and are refused by the backend for any non-loopback client. */

/** A project group returned by the backend's WorkBuddy scan. */
export interface LocalProjectGroup {
  cwd: string;
  label: string;
  sessions: LocalSessionInfo[];
}

/** A synchronizable agent source discovered on this machine. */
export interface LocalImportSource {
  source: ImportSource;
  label: string;
  root: string;
  projects: LocalProjectGroup[];
}

/** Discovery is best-effort: missing capability or remote backend → empty. */
export async function fetchLocalImportSources(): Promise<LocalImportSource[]> {
  try {
    const response = await apiFetch(apiUrl("/api/imports/local-sources"), {
      cache: "no-store",
    });
    if (!response.ok) return [];
    const data = (await response.json()) as { sources?: LocalImportSource[] };
    return data.sources ?? [];
  } catch {
    return [];
  }
}

/** Import the selected sessions by id; the server parses + persists them. */
export async function importLocalSessions(
  source: ImportSource,
  sessionIds: string[],
  agentName?: string,
): Promise<ImportResult> {
  if (sessionIds.length === 0) {
    throw new Error("No sessions to import");
  }
  const response = await apiFetch(apiUrl("/api/imports/local-import"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      source,
      agent_name: agentName ?? "",
      session_ids: sessionIds,
    }),
  });
  if (!response.ok) {
    throw new Error(`Local import failed: ${response.status}`);
  }
  const data = (await response.json()) as ImportResult;
  invalidateClientCache(IMPORTED_CACHE_PREFIX);
  return data;
}
