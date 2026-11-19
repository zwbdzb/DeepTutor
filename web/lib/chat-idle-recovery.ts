import { buildResumeTurn } from "@/contracts/parse/turn-command";

interface IdleTurnRecoveryInput {
  isStreaming: boolean;
  hasPendingUserInput: boolean;
  activeTurnId: string | null;
  lastSeq: number;
  updatedAt: number;
  now: number;
  idleTimeoutMs: number;
}

export type IdleTurnRecoveryDecision =
  | { kind: "none" }
  | { kind: "resubscribe"; message: ReturnType<typeof buildResumeTurn> }
  | { kind: "reconcile" };

/**
 * Decide what the client-side idle watchdog should do.
 *
 * A quiet WebSocket is not proof that a server turn failed. Long research
 * tool calls can legitimately emit nothing for several minutes, and the
 * backend keeps the turn alive when a browser briefly disconnects. When a
 * server turn id is known, re-subscribe from the last received sequence so
 * buffered events (including a missed terminal event) are replayed.
 */
export function decideIdleTurnRecovery(
  input: IdleTurnRecoveryInput,
): IdleTurnRecoveryDecision {
  if (!input.isStreaming || input.hasPendingUserInput) return { kind: "none" };
  if (input.now - input.updatedAt <= input.idleTimeoutMs) {
    return { kind: "none" };
  }
  if (!input.activeTurnId) return { kind: "reconcile" };
  return {
    kind: "resubscribe",
    message: buildResumeTurn({
      turnId: input.activeTurnId,
      afterSeq: input.lastSeq,
    }),
  };
}

/**
 * Whether a stored `running` status still describes a live turn.
 *
 * The backend sets a session's status to `running` when a turn starts and
 * clears it when the turn reaches a terminal state. A process that dies
 * mid-turn — a crash, a restart, a killed dev server — never gets to clear
 * it, so the row keeps saying `running` forever. Opening such a session used
 * to put the whole surface into "answering": the composer showed Stop instead
 * of Send, so the learner could not say anything, and nothing was streaming
 * for them to stop. The idle watchdog could not rescue it either, because
 * loading a session stamps `updatedAt` with the load time, which makes a
 * four-day-old turn look freshly active on every tick.
 *
 * Recency is what separates the two cases. A turn that really is running was
 * touched moments ago — the same signal the idle watchdog already trusts, so
 * this reuses its window (Settings › Network) rather than inventing a second
 * notion of "too quiet". Past that window, the status is stale bookkeeping
 * rather than a live turn, and the honest local answer is `idle`: we do not
 * know whether it finished or failed, only that it is not happening now.
 */
export function resolveLoadedRunStatus<T extends string>(
  status: T,
  lastActivityAt: number,
  now: number,
  idleTimeoutMs: number,
): T | "idle" {
  if (status !== "running") return status;
  // No usable timestamp: trust the server rather than guessing.
  if (!Number.isFinite(lastActivityAt) || lastActivityAt <= 0) return status;
  if (now - lastActivityAt <= idleTimeoutMs) return status;
  return "idle";
}

/** Statuses a `turns` row carries while the server still owns the turn. */
const LIVE_TURN_STATUSES = new Set(["queued", "running", "waiting_input"]);

/**
 * Whether an `active_turns[0]` row still describes a turn the server owns.
 * The store lists nonterminal rows only, so a present row is live by
 * construction. An absent status comes from older payloads we cannot read —
 * not a finished turn.
 */
export function isLiveTurnRowStatus(status: string | null | undefined): boolean {
  const value = String(status ?? "").trim().toLowerCase();
  return value === "" || LIVE_TURN_STATUSES.has(value);
}

/**
 * Whether that live row is producing output — the states where the
 * conversation must read as busy (Stop instead of Send). `waiting_input` is
 * excluded on purpose: a parked turn is answered through its card, and
 * advertising it as busy would let a cancel kill a turn waiting for a person.
 */
export function turnRowHoldsConversation(
  status: string | null | undefined,
): boolean {
  const value = String(status ?? "").trim().toLowerCase();
  return value === "queued" || value === "running";
}
