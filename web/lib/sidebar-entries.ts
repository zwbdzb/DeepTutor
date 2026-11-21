/**
 * What the sidebar's history region lists, and in what order.
 *
 * The region used to be three stacked tables — a "Chat" heading over the home
 * conversations, then one heading per mastery topic, then one per reading
 * collection — so a conversation held ten minutes ago sat below headings
 * whose newest conversation was a week old, and the surface you were last
 * working in was never the thing at the top.
 *
 * Here there is one list instead. A home conversation is an entry; a workspace
 * folder, a mastery topic and a reading collection are *also* entries — a
 * single collapsible unit sitting at the same level as a conversation, ranked
 * by the newest conversation inside it. So the list reads by recency all the
 * way down whatever surface the work happened on, and one hand-arranged order
 * (see ``applyManualOrder``) covers every kind.
 *
 * Folder grouping is opt-in (``groupByWorkspace``): the account-wide sidebar
 * turns it on, while a list that already shows one workspace's conversations
 * leaves it off — there a folder heading would only repeat the workspace the
 * reader is already inside. Conversations in the default workspace are never
 * wrapped in a "Default" folder: the heading would contain most of a new
 * learner's history and name nothing.
 *
 * Pure and free of React so the ordering can be tested on its own.
 */

import type { MasteryTopicLabel } from "@/lib/learning-api";
import { masteryPathIdOf, readingWorkspaceIdOf } from "@/lib/mastery-session";
import type { ReadingCollectionLabel } from "@/lib/reading-workspace-api";
import type { ChatWorkspaceRegistration } from "@/lib/workspaces-api";
import type { SessionSummary } from "@/lib/session-api";
import { sessionWorkspaceId } from "@/lib/session-api";
import { applyManualOrder } from "@/lib/sidebar-layout";

/** Which surface a group entry stands for. Decides its mark and menu. */
export type SidebarGroupKind = "workspace" | "mastery" | "reading" | "recent";

export interface SidebarSessionEntry {
  kind: "session";
  /** The session id — also this entry's key in the hand-arranged order. */
  id: string;
  session: SessionSummary;
}

export interface SidebarGroupEntry {
  kind: "group";
  /**
   * Stable id. For workspace groups it is `workspace:<workspace_id>`; for
   * mastery/reading groups it is the topic/collection id unchanged.
   */
  id: string;
  group: SidebarGroupKind;
  /** Display label shown in the heading. */
  label: string;
  /** The conversations inside, newest first. */
  rows: SessionSummary[];
  /** The workspace this folder stands for; only on workspace groups. */
  workspaceId?: string;
  /** "ready" or "invalid" (path failure); only on workspace groups. */
  workspaceStatus?: "ready" | "invalid" | string;
  /** Human-readable reason when workspaceStatus !== "ready". */
  workspaceError?: string;
}

export type SidebarEntry = SidebarSessionEntry | SidebarGroupEntry;

export interface SidebarEntriesInput {
  /** Root conversations, already in the order they should be listed. */
  roots: readonly SessionSummary[];
  /** File every conversation under one "Recent" heading instead of its own groups. */
  recents?: boolean;
  /** Topics whose study conversations get their own entry. Omit for none. */
  masteryTopics?: readonly MasteryTopicLabel[];
  /** Collections whose reading conversations get their own entry. */
  readingCollections?: readonly ReadingCollectionLabel[];
  /**
   * File conversations held in a named workspace under that workspace's
   * folder, and show registered folders even before they have a conversation.
   */
  groupByWorkspace?: boolean;
  /** Every registered workspace, for folder names, status and empty folders. */
  workspaces?: readonly ChatWorkspaceRegistration[];
  /** Hand-arranged order over the entries, by entry id. */
  manualOrder?: readonly string[];
}

interface Labels {
  recents: boolean;
  groupByWorkspace: boolean;
  topics: ReadonlyMap<string, MasteryTopicLabel>;
  collections: ReadonlyMap<string, ReadingCollectionLabel>;
  workspaces: ReadonlyMap<string, ChatWorkspaceRegistration>;
}

interface Container {
  id: string;
  group: SidebarGroupKind;
  label: string;
  workspaceId?: string;
}

function workspaceKey(workspaceId: string): string {
  return `workspace:${workspaceId}`;
}

const DEFAULT_WORKSPACE_GROUP_ID = "workspace:default";

/**
 * The group a conversation belongs in, or null when it stands on its own.
 *
 * Study conversations file under their topic, and reading conversations under
 * their collection, unless folder grouping is on and they live in a named
 * workspace — then the folder replaces the topic heading, because a folder
 * already sits inside one topic's worth of material. If its label is gone, a
 * conversation is listed on its own.
 *
 * Archived folders are demoted rather than hidden: their conversations are
 * still readable, they just cannot host a new one (and the account index does
 * not poll them, so this branch normally only sees historical rows).
 */
function containerOf(session: SessionSummary, labels: Labels): Container | null {
  if (labels.recents) {
    return { id: "recent:unassigned", group: "recent", label: "Recent" };
  }
  if (labels.groupByWorkspace) {
    const workspaceId = sessionWorkspaceId(session);
    if (workspaceId) {
      const registered = labels.workspaces.get(workspaceId);
      const archived =
        Boolean(session.content_workspace_archived) ||
        Boolean(registered?.archived);
      if (!archived) {
        return {
          id: workspaceKey(workspaceId),
          group: "workspace",
          // The catalog is authoritative: a session row may have been read
          // before the folder was renamed, or may not carry the name at all.
          label:
            registered?.display_name ||
            session.content_workspace_name ||
            workspaceId,
          workspaceId,
        };
      }
    }
    // Fallback: no workspace binding → the default folder. An archived
    // workspace also lands here so it does not vanish from the sidebar.
    return {
      id: DEFAULT_WORKSPACE_GROUP_ID,
      group: "workspace",
      label: "Default workspace",
      workspaceId: "",
    };
  }
  const topicId = masteryPathIdOf(session);
  if (topicId) {
    const topic = labels.topics.get(topicId);
    return topic ? { id: topicId, group: "mastery", label: topic.name } : null;
  }
  const collectionId = readingWorkspaceIdOf(session);
  if (collectionId) {
    const collection = labels.collections.get(collectionId);
    return collection
      ? { id: collectionId, group: "reading", label: collection.title }
      : null;
  }
  return null;
}

/** The status travellers for a fresh folder heading. */
function workspaceStatusOf(
  workspaceId: string,
  session: SessionSummary,
  workspaces: ReadonlyMap<string, ChatWorkspaceRegistration>,
): Pick<SidebarGroupEntry, "workspaceStatus" | "workspaceError"> {
  const registered = workspaces.get(workspaceId);
  return {
    workspaceStatus:
      registered?.status || session.content_workspace_status || "ready",
    workspaceError: registered?.error || session.content_workspace_error || "",
  };
}

/**
 * One flat, recency-ordered list of conversations and groups.
 *
 * A group takes the slot of the newest conversation in it, so an active folder
 * outranks a stale chat and a stale folder sinks below today's conversation —
 * which is the whole point of putting the two at the same level. Within a
 * group the rows keep the order they arrived in. Empty folders have no recency
 * to rank by, so they follow the recency-ordered entries instead of pushing
 * today's conversation down on every refresh.
 */
export function buildSidebarEntries({
  roots,
  recents = false,
  masteryTopics = [],
  readingCollections = [],
  groupByWorkspace = false,
  workspaces = [],
  manualOrder = [],
}: SidebarEntriesInput): SidebarEntry[] {
  const byId = new Map(workspaces.map((workspace) => [workspace.workspace_id, workspace]));
  const labels: Labels = {
    recents,
    groupByWorkspace,
    topics: new Map(masteryTopics.map((topic) => [topic.path_id, topic])),
    collections: new Map(
      readingCollections.map((collection) => [
        collection.workspace_id,
        collection,
      ]),
    ),
    workspaces: byId,
  };

  const entries: SidebarEntry[] = [];
  const openGroups = new Map<string, SidebarGroupEntry>();

  for (const session of roots) {
    const container = containerOf(session, labels);
    if (!container) {
      entries.push({ kind: "session", id: session.session_id, session });
      continue;
    }
    const open = openGroups.get(container.id);
    if (open) {
      open.rows.push(session);
      continue;
    }
    const entry: SidebarGroupEntry = {
      kind: "group",
      ...container,
      rows: [session],
    };
    if (container.group === "workspace" && container.workspaceId) {
      Object.assign(
        entry,
        workspaceStatusOf(container.workspaceId, session, byId),
      );
    }
    openGroups.set(container.id, entry);
    entries.push(entry);
  }

  // A folder the learner just created has no conversation to rank it by yet.
  // Show it anyway, after the recency-ordered list, so the sidebar does not
  // silently swallow the folder they are about to use.
  if (labels.groupByWorkspace && !labels.recents) {
    for (const workspace of workspaces) {
      if (workspace.archived || !workspace.workspace_id) continue;
      const id = workspaceKey(workspace.workspace_id);
      if (openGroups.has(id)) continue;
      entries.push({
        kind: "group",
        id,
        group: "workspace",
        label: workspace.display_name || workspace.workspace_id,
        rows: [],
        workspaceId: workspace.workspace_id,
        workspaceStatus: workspace.status || "ready",
        workspaceError: workspace.error || "",
      });
    }

    // The default workspace is pre-built too, exactly like the named folders.
    // Otherwise it only appears when the current category's rows happen to
    // include an unbound conversation — so it vanished on shelves (subagents,
    // skill connectors, learning space) whose rows are filtered to other
    // kinds, while the named folders stayed. Keep it always visible; the
    // forced-to-bottom pass below fixes its position.
    if (!openGroups.has(DEFAULT_WORKSPACE_GROUP_ID)) {
      entries.push({
        kind: "group",
        id: DEFAULT_WORKSPACE_GROUP_ID,
        group: "workspace",
        label: "Default workspace",
        rows: [],
        workspaceId: "",
        workspaceStatus: "ready",
        workspaceError: "",
      });
    }
  }

  // Workspace folders use a fixed, name-based order instead of recency. A
  // folder must never jump to the top just because a draft was created inside
  // it or it was clicked — its position stays put until the learner drags it.
  // Non-folder entries (loose conversations, mastery, reading) keep their
  // recency order; Array.sort is stable, so returning 0 preserves it.
  const stabilized = [...entries].sort((a, b) => {
    const aFolder = a.kind === "group" && a.group === "workspace" ? a : null;
    const bFolder = b.kind === "group" && b.group === "workspace" ? b : null;
    if (aFolder && bFolder) {
      if (aFolder.id === DEFAULT_WORKSPACE_GROUP_ID) return 1;
      if (bFolder.id === DEFAULT_WORKSPACE_GROUP_ID) return -1;
      return aFolder.label.localeCompare(bFolder.label, undefined, {
        sensitivity: "base",
      });
    }
    if (aFolder) return -1;
    if (bFolder) return 1;
    return 0;
  });

  const ordered = applyManualOrder(stabilized, (entry) => entry.id, manualOrder);

  // 默认工作区 bucket 永远压底，不参与手动排序——它是"兜底"，
  // 混在其他命名 workspace 中间看起来像是被手动排到那的。
  const defaultIdx = ordered.findIndex(
    (e) => e.kind === "group" && e.id === DEFAULT_WORKSPACE_GROUP_ID,
  );
  if (defaultIdx > 0) {
    const [def] = ordered.splice(defaultIdx, 1);
    ordered.push(def);
  }

  return ordered;
}
