"use client";

import { activeWorkspaceId, navigateTask } from "@/lib/workspace-scope";
import { sessionWorkspaceId } from "@/lib/session-api";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useRouter } from "next/navigation";
import { useTranslation } from "react-i18next";
import { LayoutSidebarShell } from "@/features/ui-layout/slots";
import { reconcileUnread } from "@/lib/session-unread";
import { LogoutButton } from "@/components/auth/LogoutButton";
import { AdminLink } from "@/components/auth/AdminLink";
import { ProfileLink } from "@/components/auth/ProfileLink";
import { useChatStateAdapter } from "@/features/chat/ChatStateAdapter";
import {
  deleteSession,
  getSession,
  listAllSessions,
  updateSessionOrganization,
  updateSessionTitle,
  type SessionDetail,
  type SessionOrganizationPatch,
  type SessionSummary,
} from "@/lib/session-api";
import {
  fetchReadingCollectionIndex,
  type ReadingCollectionLabel,
} from "@/lib/reading-workspace-api";
import {
  fetchMasteryTopicIndex,
  type MasteryTopicLabel,
} from "@/lib/learning-api";
import { readingWorkspaceIdOf, sessionRoute } from "@/lib/mastery-session";
import { readingCollectionRoute } from "@/lib/learning-routes";
import { subscribeSessionChanges } from "@/lib/session-events";
import { systemConfirm } from "@/lib/system-confirm";

/** Imported conversations the reader has opened, kept in the sidebar list. */
const OPENED_IMPORTS_STORAGE_KEY = "deeptutor.sidebar.openedImports";
const MAX_OPENED_IMPORTS = 20;

function readOpenedImportIds(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const parsed: unknown = JSON.parse(
      window.localStorage.getItem(OPENED_IMPORTS_STORAGE_KEY) || "[]",
    );
    return Array.isArray(parsed)
      ? parsed
          .filter((value): value is string => typeof value === "string")
          .slice(0, MAX_OPENED_IMPORTS)
      : [];
  } catch {
    return [];
  }
}

function writeOpenedImportIds(ids: readonly string[]) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      OPENED_IMPORTS_STORAGE_KEY,
      JSON.stringify(ids.slice(0, MAX_OPENED_IMPORTS)),
    );
  } catch {
    // Storage may be unavailable (private mode); the list still works in memory.
  }
}

function toSessionSummary(detail: SessionDetail): SessionSummary {
  return {
    id: detail.id,
    session_id: detail.session_id,
    title: detail.title,
    created_at: detail.created_at,
    updated_at: detail.updated_at,
    message_count: detail.messages.length,
    last_message: "",
    status: detail.status,
    preferences: detail.preferences,
  };
}

export default function WorkspaceSidebar() {
  const { t } = useTranslation();
  const router = useRouter();
  const {
    newSession,
    configureSession,
    cancelStreamingTurn,
    selectedSessionId,
    sessionStatuses,
    sidebarRefreshToken,
  } = useChatStateAdapter();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  /** Imported conversations opened this session, mirrored into the list below. */
  const [mirroredImports, setMirroredImports] = useState<SessionSummary[]>([]);
  const [masteryTopics, setMasteryTopics] = useState<MasteryTopicLabel[]>([]);
  const [readingCollections, setReadingCollections] = useState<
    ReadingCollectionLabel[]
  >([]);
  const [loadingSessions, setLoadingSessions] = useState(false);
  const hasLoadedSessionsRef = useRef(false);

  const refreshSessions = useCallback(async () => {
    if (!hasLoadedSessionsRef.current) {
      setLoadingSessions(true);
    }
    try {
      // Only this workspace's conversations: switching workspace is switching
      // stores. Topic labels only name a group heading, so a failure to load
      // them must not cost the session list.
      const [nextSessions, nextTopics, nextCollections] =
        await Promise.all([
          listAllSessions({ force: true }),
          fetchMasteryTopicIndex().catch(() => [] as MasteryTopicLabel[]),
          fetchReadingCollectionIndex().catch(() => [] as ReadingCollectionLabel[]),
        ]);
      setSessions(nextSessions);
      setMasteryTopics(nextTopics);
      setReadingCollections(nextCollections);
      hasLoadedSessionsRef.current = true;
    } catch (error) {
      console.error("Failed to load sessions", error);
    } finally {
      setLoadingSessions(false);
    }
  }, []);

  // First mount shows the skeleton; subsequent refreshes triggered by
  // ``sidebarRefreshToken`` (STREAM_END, server-side session bind,
  // turn deletion) silently swap in the new list. Resetting the ref
  // each refresh briefly re-renders the loading skeleton, which the
  // user perceives as a flicker on every message send / Answer Now.
  useEffect(() => {
    void refreshSessions();
  }, [refreshSessions, sidebarRefreshToken]);

  // The token above covers mutations made through the chat runtime. A restore
  // from Settings › Archive, or a delete from another surface, arrives on the
  // bus instead.
  useEffect(
    () => subscribeSessionChanges(() => void refreshSessions()),
    [refreshSessions],
  );

  // Imported conversations (ids carry the ``imported_`` prefix) are kept out of
  // the native history index on purpose — they live under Space › My Agents.
  // Whatever the reader opens is mirrored into the list and stays there, even
  // after moving to another conversation or reloading; untouched imports stay
  // in My Agents.
  useEffect(() => {
    const ids = readOpenedImportIds();
    if (ids.length === 0) return;
    let alive = true;
    void Promise.all(
      ids.map((sessionId) => getSession(sessionId).catch(() => null)),
    ).then((details) => {
      if (!alive) return;
      const rows = details
        .filter((detail): detail is SessionDetail => detail !== null)
        .map(toSessionSummary);
      if (rows.length === 0) return;
      setMirroredImports((prev) => {
        const seen = new Set(prev.map((session) => session.session_id));
        return [...prev, ...rows.filter((row) => !seen.has(row.session_id))];
      });
    });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    const sessionId = selectedSessionId || "";
    if (!sessionId.startsWith("imported_")) return;
    if (sessions.some((session) => session.session_id === sessionId)) return;
    if (mirroredImports.some((session) => session.session_id === sessionId)) return;
    let alive = true;
    void getSession(sessionId)
      .then((detail) => {
        if (!alive) return;
        const mirrored = toSessionSummary(detail);
        setMirroredImports((prev) =>
          prev.some((session) => session.session_id === mirrored.session_id)
            ? prev
            : [...prev, mirrored],
        );
      })
      .catch(() => {
        // The chat surface reports its own load failure; the list stays as is.
      });
    return () => {
      alive = false;
    };
  }, [sessions, selectedSessionId, mirroredImports]);

  useEffect(() => {
    if (mirroredImports.length === 0) return;
    writeOpenedImportIds(mirroredImports.map((session) => session.session_id));
  }, [mirroredImports]);

  const listedSessions = useMemo(() => {
    const mirrored = mirroredImports.filter(
      (row) =>
        !sessions.some((session) => session.session_id === row.session_id),
    );
    return mirrored.length ? [...mirrored, ...sessions] : sessions;
  }, [mirroredImports, sessions]);

  // What the runtime knows about turns in flight, folded onto the server's
  // list so a row's avatar spins for the turn it is actually running.
  //
  // ``sessionStatuses`` holds running sessions only, which is why it doubles as
  // the "streaming right now" set the sidebar orders by. That set is the
  // client's own view on purpose: a persisted ``status`` of "running" outlives
  // the turn that wrote it whenever a turn dies without a terminal event, and
  // ordering by it would nail a long-dead conversation to the top.
  const liveSessions = useMemo(
    () =>
      listedSessions.map((session) => {
        const runtime = sessionStatuses[session.session_id];
        return runtime
          ? {
              ...session,
              status: runtime.status,
              active_turn_id: runtime.activeTurnId || session.active_turn_id,
            }
          : session;
      }),
    [sessionStatuses, listedSessions],
  );
  const liveSessionIds = useMemo(
    () => new Set(Object.keys(sessionStatuses)),
    [sessionStatuses],
  );

  // Track which sessions finished while the reader was elsewhere. This lives
  // here rather than in a list component because the lists unmount as the
  // reader moves between surfaces, and because this is where the runtime
  // status map — the only honest source for "still running" — is held.
  useEffect(() => {
    reconcileUnread(liveSessionIds, selectedSessionId);
  }, [liveSessionIds, selectedSessionId]);

  // Starting a new chat must NOT touch whatever else is running.
  //
  // This used to call `cancelStreamingTurn()` first, on the reasoning that a
  // fresh chat should not "inherit" a still-running turn. It cannot: that
  // function cancels the *currently selected* session's turn — sending
  // `cancel_turn` to the backend and dropping its socket — so opening a new
  // chat killed the answer you had just asked for in the previous one. Ask a
  // question, start another chat, and the first conversation was left with
  // your message and no reply, permanently.
  //
  // Nothing was needed in its place. `selectFreshDraft` keys the new draft
  // separately and leaves the old entry in the map — it even refuses to evict
  // sessions whose status is `running`, which is the architecture stating
  // outright that background conversations are meant to keep going.
  const handleNewChat = useCallback(() => {
    newSession({ workspaceId: activeWorkspaceId() || null });
    navigateTask("/chat", router.push);
  }, [newSession, router]);

  // A study conversation opens on its own path, not in the main chat: the
  // outline, the waypoint header and the tutor's own composer are the context
  // it was held in, and /chat would drop all three.
  const handleSelectSession = useCallback(
    async (sessionId: string) => {
      const session = listedSessions.find((item) => item.session_id === sessionId);
      navigateTask(session ? sessionRoute(session) : `/chat/${sessionId}`, router.push);
    },
    [router, listedSessions],
  );

  const handleRenameSession = useCallback(
    async (sessionId: string, title: string) => {
      const updated = await updateSessionTitle(sessionId, title, sessionWorkspaceId(listedSessions.find(item => item.session_id === sessionId)));
      setSessions((prev) =>
        prev.map((session) =>
          session.session_id === sessionId
            ? {
                ...session,
                title: updated.title,
                updated_at: updated.updated_at,
              }
            : session,
        ),
      );
      setMirroredImports((prev) =>
        prev.map((session) =>
          session.session_id === sessionId
            ? { ...session, title: updated.title, updated_at: updated.updated_at }
            : session,
        ),
      );
    },
    [listedSessions],
  );

  const handleDeleteSession = useCallback(
    async (sessionId: string) => {
      if (!(await systemConfirm(t("Permanently delete this chat and its tutor threads? This cannot be undone.")))) return;
      const deleted = listedSessions.find(item => item.session_id === sessionId);
      await deleteSession(sessionId, sessionWorkspaceId(deleted));
      setSessions((prev) =>
        prev.filter((session) => session.session_id !== sessionId),
      );
      setMirroredImports((prev) =>
        prev.filter((session) => session.session_id !== sessionId),
      );
      writeOpenedImportIds(
        readOpenedImportIds().filter((id) => id !== sessionId),
      );
      if (selectedSessionId === sessionId) {
        cancelStreamingTurn();
        newSession({ workspaceId: activeWorkspaceId() || null });
        // A reading conversation was open beside its material; deleting it
        // should leave the reader on that material, not throw them to /chat.
        const readingId = deleted ? readingWorkspaceIdOf(deleted) : "";
        navigateTask(
          readingId
            ? readingCollectionRoute(readingId, sessionWorkspaceId(deleted))
            : "/chat",
          router.push,
        );
      }
    },
    [cancelStreamingTurn, listedSessions, newSession, router, selectedSessionId, t],
  );

  const handleOrganizeSession = useCallback(
    async (sessionId: string, patch: SessionOrganizationPatch) => {
      const updated = await updateSessionOrganization(sessionId, patch, sessionWorkspaceId(listedSessions.find(item => item.session_id === sessionId)));
      if ("workspace_id" in patch) {
        configureSession({ workspaceId: updated.preferences?.workspace_id ?? null }, sessionId);
        if (selectedSessionId === sessionId) {
          navigateTask(sessionRoute({ ...listedSessions.find(row => row.session_id === sessionId)!,
            preferences: updated.preferences, content_workspace_id: updated.preferences?.workspace_id || "" }), router.push);
        }
      }
      // A conversation moved to another workspace has left this list.
      const movedAway = "workspace_id" in patch &&
        (updated.preferences?.workspace_id || "") !== activeWorkspaceId();
      setSessions((previous) =>
        movedAway
          ? previous.filter((session) => session.session_id !== sessionId)
          : previous.map((session) =>
              session.session_id === sessionId
                ? { ...session, updated_at: updated.updated_at, preferences: updated.preferences }
                : session,
            ),
      );
    },
    [configureSession, listedSessions, selectedSessionId, router],
  );

  return (
    <LayoutSidebarShell
      showSessions
      sessions={liveSessions}
      liveSessionIds={liveSessionIds}
      masteryTopics={masteryTopics}
      readingCollections={readingCollections}
      activeSessionId={selectedSessionId}
      loadingSessions={loadingSessions}
      onNewChat={handleNewChat}
      onSelectSession={handleSelectSession}
      onRenameSession={handleRenameSession}
      onDeleteSession={handleDeleteSession}
      onOrganizeSession={handleOrganizeSession}
      footerSlot={(collapsed) => (
        <>
          <ProfileLink collapsed={collapsed} />
          <AdminLink collapsed={collapsed} />
          <LogoutButton collapsed={collapsed} />
        </>
      )}
    />
  );
}
