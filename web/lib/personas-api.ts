import { apiFetch, apiUrl } from "@/lib/api";
import { invalidateClientCache, withClientCache } from "@/lib/client-cache";
import { asJsonOrThrow as asJson } from "@/lib/api";

const PERSONAS_CACHE_PREFIX = "personas:";

export type PersonaSource = "user" | "admin";

export interface PersonaInfo {
  name: string;
  /** Optional user-facing label (may be Chinese); falls back to `name` when empty. */
  display_name: string;
  description: string;
  /**
   * Optional grouping label for the gallery's category tabs
   * (`education` / `work` / `code`). Empty means "ungrouped": such a persona
   * is listed under "All" only.
   */
  category: string;
  source: PersonaSource;
  read_only: boolean;
  /** Backend-tracked pick count for this account; drives the "Response style" ranking. */
  usage_count: number;
}

export interface PersonaDetail extends PersonaInfo {
  content: string;
}

export interface CreatePersonaPayload {
  name: string;
  description: string;
  content: string;
}

export interface UpdatePersonaPayload {
  description?: string;
  content?: string;
  rename_to?: string;
}

function normalizeSource(raw: unknown): PersonaSource {
  return raw === "admin" ? "admin" : "user";
}

function normalizeInfo(item: {
  name?: unknown;
  display_name?: unknown;
  description?: unknown;
  category?: unknown;
  source?: unknown;
  read_only?: unknown;
  usage_count?: unknown;
}): PersonaInfo {
  return {
    name: String(item?.name ?? ""),
    display_name: String(item?.display_name ?? ""),
    description: String(item?.description ?? ""),
    // Older personas (and older backends) carry no category: they simply
    // belong to no tab but "All", so an empty string is the correct default.
    category: String(item?.category ?? "").trim().toLowerCase(),
    source: normalizeSource(item?.source),
    read_only: Boolean(item?.read_only),
    usage_count:
      typeof item?.usage_count === "number" && Number.isFinite(item.usage_count)
        ? Math.max(0, Math.floor(item.usage_count))
        : 0,
  };
}

export async function listPersonas(options?: {
  force?: boolean;
}): Promise<PersonaInfo[]> {
  return withClientCache<PersonaInfo[]>(
    `${PERSONAS_CACHE_PREFIX}list`,
    async () => {
      const response = await apiFetch(apiUrl("/api/personas"), {
        cache: "no-store",
      });
      const data = await asJson(response);
      const items = Array.isArray(data?.personas) ? data.personas : [];
      return items.map(normalizeInfo);
    },
    { force: options?.force },
  );
}

export async function getPersona(name: string): Promise<PersonaDetail> {
  const response = await apiFetch(
    apiUrl(`/api/personas/${encodeURIComponent(name)}`),
    {
      cache: "no-store",
    },
  );
  const data = await asJson(response);
  return {
    ...normalizeInfo({ ...data, name: data?.name ?? name }),
    content: String(data?.content ?? ""),
  };
}

export async function createPersona(
  payload: CreatePersonaPayload,
): Promise<PersonaInfo> {
  const response = await apiFetch(apiUrl("/api/personas"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: payload.name,
      description: payload.description,
      content: payload.content,
    }),
  });
  const data = await asJson(response);
  invalidatePersonasCache();
  return normalizeInfo({ ...data, name: data?.name ?? payload.name });
}

export async function updatePersona(
  name: string,
  payload: UpdatePersonaPayload,
): Promise<PersonaInfo> {
  const response = await apiFetch(
    apiUrl(`/api/personas/${encodeURIComponent(name)}`),
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  const data = await asJson(response);
  invalidatePersonasCache();
  return normalizeInfo({ ...data, name: data?.name ?? name });
}

export async function deletePersona(name: string): Promise<void> {
  const response = await apiFetch(
    apiUrl(`/api/personas/${encodeURIComponent(name)}`),
    {
      method: "DELETE",
    },
  );
  await asJson(response);
  invalidatePersonasCache();
}

/**
 * Record that this account picked the persona, so it climbs the
 * "Response style" ranking. Best-effort: the user must not be blocked
 * when the call fails (offline, quota, …).
 */
export async function touchPersonaUsage(name: string): Promise<void> {
  try {
    await apiFetch(
      apiUrl(`/api/personas/${encodeURIComponent(name)}/touch`),
      { method: "POST" },
    );
  } finally {
    // A pick changes the backend ranking; drop the cached list so the next
    // open reflects the new order.
    invalidatePersonasCache();
  }
}

export function invalidatePersonasCache() {
  invalidateClientCache(PERSONAS_CACHE_PREFIX);
}
