"use client";

import Tooltip from "@/shared/ui/Tooltip";
import { Dialog } from "@/shared/ui/Dialog";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Check, ChevronDown, Folder, FolderOpen, Plus } from "lucide-react";
import { useTranslation } from "react-i18next";

import { useLingerExpand } from "@/hooks/use-linger-expand";
import ToolbarLabel from "@/components/chat/home/ToolbarLabel";
import { saveWorkspace, type ChatWorkspaceRegistration } from "@/lib/workspaces-api";

/**
 * Which workspace this conversation belongs to.
 *
 * Selecting a workspace is never a pure preference change; it moves the
 * conversation's storage in place (via ``updateSessionOrganization`` when a
 * session already exists) and navigates so that the runtime connection's
 * workspace scope matches the new storage location. ``readOnly`` is an opt-in
 * freeze the caller can pass for screens where mid-thread moves are still
 * undesirable (e.g. co-writer, learning modules); the chat composer always
 * leaves it unset so existing conversations can be relocated with one click.
 */
export function WorkspacePill({
  workspaces,
  workspaceId,
  onSelect,
  disabled = false,
  readOnly = false,
  collapsible = false,
  error = "",
  label: pickerLabel = "Conversation workspace",
}: {
  workspaces: ChatWorkspaceRegistration[];
  workspaceId: string;
  onSelect: (workspaceId: string) => void;
  disabled?: boolean;
  collapsible?: boolean;
  /** Freeze the binding as a label: this conversation already has messages. */
  readOnly?: boolean;
  error?: string;
  label?: string;
}) {
  const { t } = useTranslation();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [createError, setCreateError] = useState("");
  const [created, setCreated] = useState<ChatWorkspaceRegistration[]>([]);
  const [open, setOpen] = useState(false);
  const { expanded, triggerProps } = useLingerExpand(open, 1200, !collapsible);
  const menuRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const nameInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      const target = event.target as Node;
      if (
        !menuRef.current?.contains(target) &&
        !buttonRef.current?.contains(target)
      ) {
        setOpen(false);
      }
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  const handleOpenCreate = () => {
    setName("");
    setCreateError("");
    setCreating(true);
    setOpen(false);
    // Autofocus after the dialog has mounted — Dialog handles focus on
    // [data-autofocus]; we still keep a ref ready for submit-on-Enter.
    requestAnimationFrame(() => nameInputRef.current?.focus());
  };

  const handleSubmitCreate = async (event?: React.FormEvent) => {
    event?.preventDefault();
    if (!name.trim() || saving) return;
    setSaving(true);
    setCreateError("");
    try {
      const row = await saveWorkspace({ name: name.trim() });
      setCreated((items) => [...items, row]);
      onSelect(row.workspace_id);
      setCreating(false);
      setName("");
    } catch (error) {
      setCreateError(error instanceof Error ? error.message : String(error));
    } finally {
      setSaving(false);
    }
  };

  const handleCloseCreate = () => {
    if (saving) return;
    setCreating(false);
    setCreateError("");
    setName("");
  };

  const choices = [...workspaces, ...created.filter(row => !workspaces.some(item => item.workspace_id === row.workspace_id))];
  const active = choices.find((row) => row.workspace_id === workspaceId);
  // An archived workspace keeps its conversations; it just stops being offered
  // for new ones. Still listed while it is the current binding, or the menu
  // would deny the workspace the pill is naming.
  const selectable = choices.filter(
    (row) =>
      row.kind === "workspace" &&
      (!row.archived || row.workspace_id === workspaceId),
  );

  const label = active
    ? active.display_name
    : workspaceId
      ? t("Workspace unavailable")
      : t("Default workspace");

  if (readOnly) {
    return (
      <Tooltip
        label={`${t(pickerLabel)}: ${label}`}
        description={t("Conversations with messages are moved from workspace settings")}
        side="top"
      >
        <span
          {...triggerProps}
          tabIndex={collapsible ? 0 : undefined}
          className="inline-flex h-8 min-w-0 shrink-0 items-center rounded-lg px-2 text-[13px] font-medium text-[var(--muted-foreground)]"
        >
          {workspaceId ? (
            <FolderOpen size={15} strokeWidth={1.7} className="shrink-0" />
          ) : (
            <Folder size={15} strokeWidth={1.7} className="shrink-0" />
          )}
          <span className="sr-only">{label}</span>
          <ToolbarLabel expanded={expanded}>
            <span className="max-w-[150px] truncate">{label}</span>
          </ToolbarLabel>
        </span>
      </Tooltip>
    );
  }

  return (
    <div className="relative flex min-w-0 items-center gap-1.5">
      <Tooltip label={label} side="top">
        <button
          ref={buttonRef}
          {...triggerProps}
          type="button"
          disabled={disabled}
          onClick={() => setOpen((value) => !value)}
          aria-haspopup="menu"
          aria-expanded={open}
          aria-label={t(pickerLabel)}
          className="inline-flex h-8 shrink-0 items-center rounded-lg px-2 text-[13px] font-medium text-[var(--muted-foreground)] transition-[background-color,color,transform] duration-150 hover:bg-[color-mix(in_srgb,var(--muted)_55%,transparent)] hover:text-[var(--foreground)] active:scale-[0.97] disabled:opacity-50"
        >
          {workspaceId ? (
            <FolderOpen size={15} strokeWidth={1.7} className="shrink-0" />
          ) : (
            <Folder size={15} strokeWidth={1.7} className="shrink-0" />
          )}
          <ToolbarLabel expanded={expanded}>
            <span className="max-w-[150px] truncate">{label}</span>
            <ChevronDown
              size={12}
              strokeWidth={2}
              className={`-mr-0.5 shrink-0 transition-transform duration-200 ${open ? "rotate-180" : ""}`}
            />
          </ToolbarLabel>
        </button>
      </Tooltip>

      {error ? (
        <Tooltip label={`${t("Error")}: ${error}`} side="top">
          <span
            role="alert"
            tabIndex={0}
            className="max-w-[180px] truncate text-[11.5px] text-[var(--destructive)]"
          >
            {error}
          </span>
        </Tooltip>
      ) : null}

      {open && !disabled && (
        <div
          ref={menuRef}
          role="menu"
          className="dt-popup-up absolute bottom-full left-0 z-50 mb-1.5 w-[248px] overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--popover)] py-1 shadow-lg backdrop-blur-md"
        >
          <div className="max-h-[280px] overflow-y-auto [&>span]:w-full">
            {selectable.map((row) => (
              <Tooltip
                key={row.workspace_id}
                label={row.status === "ready" ? row.path : row.error}
                side="right"
              >
                <button
                  type="button"
                  role="menuitemradio"
                  aria-checked={row.workspace_id === workspaceId}
                  disabled={row.status !== "ready"}
                  onClick={() => {
                    onSelect(row.workspace_id);
                    setOpen(false);
                  }}
                  className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12.5px] text-[var(--foreground)] transition-colors hover:bg-[color-mix(in_srgb,var(--muted)_60%,transparent)] disabled:opacity-50"
                >
                  <Folder
                    size={13}
                    strokeWidth={1.7}
                    className="shrink-0 text-[var(--muted-foreground)]"
                  />
                  <span className="min-w-0 flex-1 truncate">
                    {row.display_name}
                  </span>
                  {row.workspace_id === workspaceId ? (
                    <Check size={12} className="shrink-0" />
                  ) : null}
                </button>
              </Tooltip>
            ))}
            <button
              type="button"
              role="menuitemradio"
              aria-checked={!workspaceId}
              onClick={() => {
                onSelect("");
                setOpen(false);
              }}
              className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12.5px] text-[var(--foreground)] transition-colors hover:bg-[color-mix(in_srgb,var(--muted)_60%,transparent)]"
            >
              <Folder
                size={13}
                strokeWidth={1.7}
                className="shrink-0 text-[var(--muted-foreground)]"
              />
              <span className="min-w-0 flex-1 truncate">
                {t("Default workspace")}
              </span>
              {!workspaceId ? <Check size={12} className="shrink-0" /> : null}
            </button>
          </div>

          <div className="mt-1 flex items-center gap-1 border-t border-[color-mix(in_srgb,var(--border)_70%,transparent)] px-2 pt-1 text-[var(--muted-foreground)]">
            <Link
              href="/settings/workspace"
              onClick={() => setOpen(false)}
              className="min-w-0 flex-1 rounded-md px-1 py-1.5 text-[11.5px] transition-colors hover:bg-[var(--muted)] hover:text-[var(--foreground)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--ring)]"
            >
              {t("Manage workspaces")}
            </Link>
            <Tooltip label={t("New workspace")} side="top">
              <button
                type="button"
                aria-label={t("New workspace")}
                onClick={handleOpenCreate}
                className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md transition-colors hover:bg-[var(--muted)] hover:text-[var(--foreground)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--ring)]"
              >
                <Plus size={15} strokeWidth={1.7} />
              </button>
            </Tooltip>
          </div>
        </div>
      )}

      {/* New workspace modal — replaces the old inline form so the menu stays
          small and the input gets its own focused surface (matches PRD
          folder.group / WorkBuddy pattern). */}
      <Dialog
        open={creating}
        title={t("folder.group.create")}
        description={t("folder.group.nameHint")}
        onClose={handleCloseCreate}
        busy={saving}
        size="sm"
        footer={
          <>
            <button
              type="button"
              disabled={saving}
              onClick={handleCloseCreate}
              className="rounded-md border border-[var(--border)] bg-transparent px-3 py-1.5 text-sm font-medium text-[var(--foreground)] transition-colors hover:bg-[var(--muted)] disabled:opacity-50"
            >
              {t("folder.group.cancel")}
            </button>
            <button
              type="button"
              disabled={saving || !name.trim()}
              onClick={() => handleSubmitCreate()}
              className="rounded-md bg-[var(--primary)] px-3 py-1.5 text-sm font-medium text-[var(--primary-foreground)] transition-opacity hover:opacity-90 disabled:opacity-50"
            >
              {saving ? t("Creating…") : t("folder.group.confirm")}
            </button>
          </>
        }
      >
        <form onSubmit={handleSubmitCreate} className="space-y-3">
          <label className="block">
            <span className="mb-1 block text-sm font-medium text-[var(--foreground)]">
              {t("folder.group.nameLabel")}
            </span>
            <input
              ref={nameInputRef}
              data-autofocus
              type="text"
              required
              maxLength={100}
              value={name}
              onChange={(event) => setName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  handleSubmitCreate();
                }
              }}
              placeholder={t("folder.group.namePlaceholder")}
              className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-2 text-sm text-[var(--foreground)] placeholder:text-[var(--muted-foreground)]/60 focus:border-[var(--ring)] focus:outline-none focus:ring-2 focus:ring-[var(--ring)]/40"
            />
          </label>
          {createError ? (
            <p role="alert" className="text-xs text-[var(--destructive)]">
              {createError}
            </p>
          ) : null}
          <p className="text-xs text-[var(--muted-foreground)]">
            {t("folder.group.nameHint")}
          </p>
        </form>
      </Dialog>
    </div>
  );
}
