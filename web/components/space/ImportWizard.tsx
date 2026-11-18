"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  // useRef,
  useState,
  // type ChangeEvent,
  type ReactNode,
} from "react";
import {
  AlertTriangle,
  CheckCircle2,
  // Eye,
  // FileJson,
  FolderInput,
  FolderOpen,
  Loader2,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import Modal from "@/components/common/Modal";
import Button from "@/components/ui/Button";
import ScopePicker from "@/components/space/ScopePicker";
import {
  fetchLocalImportSources,
  importChatHistory,
  // importChatHistoryInBatches,
  importLocalSessions,
  type LocalImportSource,
} from "@/lib/imports-api";
import { newAgentId, saveAgent } from "@/lib/chat-import/agent-store";
import {
  buildSelectGroups,
  // ImportScanError,
  // isFileSystemAccessSupported,
  // parseChatGptExportFile,
  parseSessions,
  // pickAndScan,
  projectLabel,
  selectionUnit,
  SOURCE_LABEL,
  type AgentScope,
  type ImportScanErrorCode,
  type ImportSource,
  type PickableSession,
  type ScanResult,
  type SelectGroup,
  type SelectionUnit,
} from "@/lib/chat-import";

/** Selection-row shape produced by the local backend scan (no FS handle). */
export interface PickerGroup {
  key: string;
  label: string;
  sublabel?: string;
  sessions: PickableSession[];
}

type Phase = "intro" | "scanning" | "select" | "importing" | "done" | "error";

interface ImportWizardProps {
  onClose: () => void;
  onImported: () => void;
}

function buildScope(unit: SelectionUnit, keys: string[]): AgentScope {
  return unit === "dates"
    ? { kind: "dates", days: keys }
    : { kind: "projects", cwds: keys };
}

export default function ImportWizard({
  onClose,
  onImported,
}: ImportWizardProps) {
  const { t, i18n } = useTranslation();
  // 原代码：const chatGptFileRef = useRef<HTMLInputElement>(null);
  const [phase, setPhase] = useState<Phase>("intro");
  // 第三方（Claude/Codex/ChatGPT 文件夹与导出）导入已隐藏，runScan/setScan 一并退休；
  // 恢复时改回 const [scan, setScan] = useState<ScanResult | null>(null);
  const [scan] = useState<ScanResult | null>(null);
  const [name, setName] = useState("");
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set());
  const [errorCode, setErrorCode] = useState<ImportScanErrorCode | "generic">(
    "generic",
  );
  const [progress, setProgress] = useState({
    stage: "parsing" as "parsing" | "saving",
    done: 0,
    total: 0,
  });
  const [result, setResult] = useState<{ imported: number; skipped: number }>({
    imported: 0,
    skipped: 0,
  });

  // Local (desktop) source flow: the backend scans ~/.workbuddy for us, so no
  // File System handle is involved — selection and import go through the API.
  const [localSources, setLocalSources] = useState<LocalImportSource[]>([]);
  const [localSource, setLocalSource] = useState<LocalImportSource | null>(null);
  const [localGroups, setLocalGroups] = useState<PickerGroup[]>([]);
  const [isLocal, setIsLocal] = useState(false);

  useEffect(() => {
    let alive = true;
    void fetchLocalImportSources()
      .then((sources) => {
        if (alive) setLocalSources(sources);
      })
      .catch(() => {
        // discovery is best-effort; absent capability just hides the button
      });
    return () => {
      alive = false;
    };
  }, []);

  const groups = useMemo<SelectGroup[] | PickerGroup[]>(
    () =>
      isLocal ? localGroups : scan ? buildSelectGroups(scan) : [],
    [scan, isLocal, localGroups],
  );
  const unit: SelectionUnit = scan ? selectionUnit(scan.source) : "projects";
  // Browser path: fully-parsable refs (with a real file handle).
  const selectedRefs = useMemo<ScanResult["projects"][number]["sessions"]>(
    () =>
      isLocal
        ? []
        : (groups as SelectGroup[])
            .filter((g) => selectedKeys.has(g.key))
            .flatMap((g) => g.sessions),
    [groups, selectedKeys, isLocal],
  );
  // Local path: only the external ids are needed; the server does the parsing.
  const selectedIds = useMemo(() => {
    if (!isLocal) return [];
    return (groups as PickerGroup[])
      .filter((g) => selectedKeys.has(g.key))
      .flatMap((g) => g.sessions.map((s) => s.externalId));
  }, [groups, selectedKeys, isLocal]);
  const selectedCount = isLocal ? selectedIds.length : selectedRefs.length;

  /* 原代码（第三方文件夹导入，已隐藏）：
  const runScan = useCallback(async () => {
    setIsLocal(false);
    setLocalSource(null);
    setPhase("scanning");
    try {
      const next = await pickAndScan();
      const nextGroups = buildSelectGroups(next);
      setScan(next);
      // Default to "bring everything in" — one click for the common case, with
      // every unit pre-checked so narrowing is just deselecting.
      setSelectedKeys(new Set(nextGroups.map((g) => g.key)));
      setName(SOURCE_LABEL[next.source]);
      setPhase("select");
    } catch (err) {
      if (err instanceof ImportScanError && err.code === "aborted") {
        setPhase("intro");
        return;
      }
      setErrorCode(err instanceof ImportScanError ? err.code : "generic");
      setPhase("error");
    }
  }, []);
  */

  /** Start the local backend-scanned flow (e.g. WorkBuddy on this machine). */
  const runLocalStart = useCallback((source: LocalImportSource) => {
    const nextGroups: PickerGroup[] = source.projects.map((p) => ({
      key: p.cwd,
      label: p.label || projectLabel(p.cwd),
      sublabel: p.cwd,
      sessions: p.sessions.map((s) => ({
        externalId: s.external_id,
        provisionalTitle: s.title || "",
        lastModified: s.last_modified,
      })),
    }));
    setLocalSource(source);
    setLocalGroups(nextGroups);
    setIsLocal(true);
    setSelectedKeys(new Set(nextGroups.map((g) => g.key)));
    setName(source.label || "WorkBuddy");
    setPhase("select");
  }, []);

  /* 原代码（ChatGPT 导出导入，已隐藏）：
  const runChatGptImport = useCallback(async (file: File) => {
    setPhase("importing");
    setProgress({ stage: "parsing", done: 0, total: 0 });
    try {
      const normalized = await parseChatGptExportFile(file, (done, total) =>
        setProgress({ stage: "parsing", done, total }),
      );
      setProgress({ stage: "saving", done: 0, total: normalized.length });
      const res = await importChatHistoryInBatches("chatgpt", normalized, {
        onProgress: (done, total) =>
          setProgress({ stage: "saving", done, total }),
      });
      setResult({ imported: res.imported, skipped: res.skipped });
      setPhase("done");
    } catch (err) {
      setErrorCode(err instanceof ImportScanError ? err.code : "generic");
      setPhase("error");
    }
  }, []);

  const handleChatGptFile = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      const file = event.target.files?.[0];
      // Selecting the same export again must still fire a change event after a
      // failed or partial import.
      event.target.value = "";
      if (file) void runChatGptImport(file);
    },
    [runChatGptImport],
  );
  */

  const toggleKey = useCallback((key: string) => {
    setSelectedKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const selectAll = useCallback(
    () => setSelectedKeys(new Set(groups.map((g) => g.key))),
    [groups],
  );
  const clearAll = useCallback(() => setSelectedKeys(new Set()), []);

  const runImport = useCallback(async () => {
    if (isLocal) {
      if (!localSource || selectedIds.length === 0) return;
      setPhase("importing");
      setProgress({ stage: "saving", done: 0, total: selectedIds.length });
      try {
        const res = await importLocalSessions(
          localSource.source,
          selectedIds,
          name.trim() || localSource.label || "WorkBuddy",
        );
        setResult({ imported: res.imported, skipped: res.skipped });
        setPhase("done");
      } catch {
        setErrorCode("generic");
        setPhase("error");
      }
      return;
    }
    if (!scan || selectedRefs.length === 0) return;
    setPhase("importing");
    setProgress({ stage: "parsing", done: 0, total: selectedRefs.length });
    try {
      const normalized = await parseSessions(
        scan.source,
        selectedRefs,
        (done, total) => setProgress({ stage: "parsing", done, total }),
      );
      setProgress({ stage: "saving", done: 0, total: normalized.length });
      const scope = buildScope(unit, [...selectedKeys]);
      const agentId = newAgentId(scan.source);
      const agentName = name.trim() || SOURCE_LABEL[scan.source];
      const res = await importChatHistory(scan.source, normalized, {
        id: agentId,
        name: agentName,
      });
      // Register the named, scoped agent so it can be re-synced later without
      // re-picking. Never block the success state on the registry write.
      try {
        await saveAgent({
          id: agentId,
          name: agentName,
          source: scan.source,
          folderName: scan.handle.name,
          handle: scan.handle,
          scope,
          createdAt: Date.now(),
          lastSyncAt: Date.now(),
        });
      } catch {
        // registry is best-effort
      }
      setResult({ imported: res.imported, skipped: res.skipped });
      setPhase("done");
    } catch {
      setErrorCode("generic");
      setPhase("error");
    }
  }, [scan, selectedRefs, selectedKeys, unit, name, isLocal, localSource, selectedIds]);

  const titleIcon = <FolderInput className="h-[18px] w-[18px]" />;

  return (
    <Modal
      isOpen
      onClose={onClose}
      title={t("Import conversations")}
      titleIcon={titleIcon}
      width="xl"
      closeOnBackdrop={phase !== "importing"}
      closeOnEscape={phase !== "importing"}
      showCloseButton={phase !== "importing"}
      footer={
        <WizardFooter
          phase={phase}
          source={scan?.source ?? (localSource ? localSource.source : null)}
          selectedCount={selectedCount}
          canImport={selectedCount > 0}
          onCancel={onClose}
          // 原代码：onPick={runScan}
          // 原代码：onPickChatGpt={() => chatGptFileRef.current?.click()}
          onImport={runImport}
          onDone={onImported}
          onRetry={() => setPhase("intro")}
        />
      }
    >
      <div className="px-5 py-5">
        {/* 原代码（ChatGPT 导出文件选择，已隐藏）：
        <input
          ref={chatGptFileRef}
          type="file"
          accept=".json,application/json"
          className="hidden"
          onChange={handleChatGptFile}
        />
        */}
        {phase === "intro" && (
          <IntroView
            localSources={localSources}
            onPickLocal={
              localSources.length
                ? () => runLocalStart(localSources[0])
                : undefined
            }
          />
        )}
        {phase === "scanning" && (
          <CenteredStatus
            icon={<Loader2 className="h-5 w-5 animate-spin" />}
            title={t("Reading your folder…")}
            subtitle={t("Finding projects and conversations.")}
          />
        )}
        {phase === "select" &&
          (scan || isLocal) &&
          (groups.length === 0 ? (
            <CenteredStatus
              icon={<FolderOpen className="h-5 w-5" />}
              title={t("No conversations found")}
              subtitle={t(
                "This folder has no readable chat history. Pick your .claude or .codex folder.",
              )}
            />
          ) : (
            <div className="space-y-4">
              <div>
                <label
                  htmlFor="agent-name"
                  className="mb-1.5 block text-[11px] font-semibold uppercase tracking-[0.12em] text-[var(--muted-foreground)]"
                >
                  {t("Agent name")}
                </label>
                <input
                  id="agent-name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder={SOURCE_LABEL[scan?.source ?? "workbuddy"]}
                  className="w-full rounded-xl border border-[var(--border)] bg-[var(--background)] px-3 py-2 text-[13px] text-[var(--foreground)] outline-none transition focus:border-[var(--primary)]/50 focus:ring-2 focus:ring-[var(--primary)]/15"
                />
                <p className="mt-1.5 text-[11px] leading-relaxed text-[var(--muted-foreground)]">
                  {unit === "dates"
                    ? t(
                        "Pick the days to bring in. Refreshing later re-syncs these days and pulls in new conversations on them.",
                      )
                    : t(
                        "Pick the projects to bring in. Refreshing later re-syncs these projects and pulls in their new conversations.",
                      )}
                </p>
              </div>
              <ScopePicker
                groups={groups}
                unit={unit}
                selected={selectedKeys}
                onToggle={toggleKey}
                onSelectAll={selectAll}
                onClearAll={clearAll}
                lang={i18n.language}
              />
            </div>
          ))}
        {phase === "importing" && <ImportingView progress={progress} />}
        {phase === "done" && <DoneView result={result} />}
        {phase === "error" && <ErrorView code={errorCode} />}
      </div>
    </Modal>
  );
}

/* ----------------------------- sub-views ----------------------------- */

function IntroView({
  localSources,
  onPickLocal,
}: {
  localSources: LocalImportSource[];
  onPickLocal?: () => void;
}) {
  const { t } = useTranslation();
  // 原代码：const supported = isFileSystemAccessSupported();
  const localCount = localSources.reduce(
    (n, source) =>
      n + source.projects.reduce((m, p) => m + p.sessions.length, 0),
    0,
  );
  return (
    <div className="space-y-5">
      <div className="flex items-center gap-3.5">
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-[var(--border)]/60 bg-[var(--card)] text-[var(--foreground)] shadow-sm">
          <FolderInput size={16} strokeWidth={1.6} />
        </span>
        <div className="min-w-0 space-y-1">
          <p className="text-[13px] leading-relaxed text-[var(--foreground)]">
            {/* 原代码（第三方导入说明，已隐藏）：
            {t("Import a local Claude Code or Codex folder, or choose the conversations.json file from an official ChatGPT data export. DeepTutor parses it in your browser before saving the conversations you import.")}
            {t("Folder imports remain refreshable agents. ChatGPT exports are one-time snapshots that can be imported again safely without duplicating conversations.")}
            */}
            {t(
              "Import WorkBuddy conversations saved on this computer, then open any conversation to continue it in DeepTutor.",
            )}
          </p>
        </div>
      </div>

      {localSources.length > 0 && onPickLocal ? (
        <div className="flex items-center justify-between gap-3 rounded-xl border border-[var(--primary)]/25 bg-[var(--primary)]/[0.05] px-3.5 py-3">
          <div className="min-w-0 space-y-0.5">
            <p className="text-[13px] font-medium text-[var(--foreground)]">
              {t("WorkBuddy history found on this computer")}
            </p>
            <p className="text-[12px] text-[var(--muted-foreground)]">
              {t("Found {{count}} WorkBuddy conversations — import them here, no folder picking needed.", {
                count: localCount,
              })}
            </p>
          </div>
          <Button
            variant="secondary"
            size="sm"
            icon={<FolderOpen className="h-4 w-4" />}
            onClick={onPickLocal}
          >
            {t("Import WorkBuddy from this computer")}
          </Button>
        </div>
      ) : null}

      {/* 原代码（浏览器文件夹/隐藏文件夹提示，第三方导入已隐藏）：
      {!supported ? (
        <div className="flex items-start gap-2.5 rounded-xl border border-amber-200 bg-amber-50/70 px-3.5 py-3 text-[12px] leading-relaxed text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/20 dark:text-amber-300">
          <AlertTriangle className="mt-px h-4 w-4 shrink-0" />
          <span>
            {t(
              "Your browser doesn't support folder access, but you can still import a ChatGPT JSON export.",
            )}
          </span>
        </div>
      ) : (
        <div className="flex items-start gap-2.5 rounded-xl border border-[var(--border)] bg-[var(--muted)]/40 px-3.5 py-3 text-[12px] leading-relaxed text-[var(--foreground)]/85">
          <Eye className="mt-px h-4 w-4 shrink-0 text-[var(--muted-foreground)]" />
          <span>
            {t(
              "Hidden folders like .claude won't show by default — press ⌘⇧. (Command-Shift-Period) in the picker to reveal them.",
            )}
          </span>
        </div>
      )}
      */}
    </div>
  );
}

function ImportingView({
  progress,
}: {
  progress: { stage: "parsing" | "saving"; done: number; total: number };
}) {
  const { t } = useTranslation();
  const pct =
    progress.total > 0
      ? Math.round((progress.done / progress.total) * 100)
      : progress.stage === "saving"
        ? 90
        : 0;
  return (
    <div className="space-y-4 py-6">
      <CenteredStatus
        icon={<Loader2 className="h-5 w-5 animate-spin" />}
        title={
          progress.stage === "parsing"
            ? t("Reading conversations…")
            : t("Saving to your space…")
        }
        subtitle={
          progress.stage === "parsing"
            ? t("{{done}} / {{total}}", {
                done: progress.done,
                total: progress.total,
              })
            : t("Almost there.")
        }
      />
      <div className="mx-auto h-1.5 w-full max-w-sm overflow-hidden rounded-full bg-[var(--muted)]/60">
        <div
          className="h-full rounded-full bg-[var(--primary)] transition-[width] duration-200"
          style={{ width: `${Math.max(6, pct)}%` }}
        />
      </div>
    </div>
  );
}

function DoneView({
  result,
}: {
  result: { imported: number; skipped: number };
}) {
  const { t } = useTranslation();
  return (
    <CenteredStatus
      icon={
        <CheckCircle2 className="h-6 w-6 text-emerald-600 dark:text-emerald-400" />
      }
      title={t("Imported {{count}} conversations", { count: result.imported })}
      subtitle={
        result.skipped > 0
          ? t("{{count}} skipped (already imported or empty).", {
              count: result.skipped,
            })
          : t("They're ready in your space — open one to keep chatting.")
      }
    />
  );
}

function ErrorView({ code }: { code: ImportScanErrorCode | "generic" }) {
  const { t } = useTranslation();
  const message =
    code === "not_recognized"
      ? t(
          "This folder doesn't look like a .claude or .codex home. Please select the right folder.",
        )
      : code === "invalid_export"
        ? t(
            "This file doesn't contain readable ChatGPT conversations. Select the conversations.json file from an official ChatGPT data export.",
          )
        : code === "unsupported_browser"
          ? t(
              "Your browser doesn't support folder access. Please use a Chromium-based browser.",
            )
          : t("Something went wrong while importing. Please try again.");
  return (
    <CenteredStatus
      icon={
        <AlertTriangle className="h-6 w-6 text-amber-600 dark:text-amber-400" />
      }
      title={t("Couldn't import")}
      subtitle={message}
    />
  );
}

function CenteredStatus({
  icon,
  title,
  subtitle,
}: {
  icon: ReactNode;
  title: string;
  subtitle: string;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-10 text-center">
      <span className="flex h-11 w-11 items-center justify-center rounded-2xl border border-[var(--border)]/60 bg-[var(--card)] text-[var(--muted-foreground)] shadow-sm">
        {icon}
      </span>
      <div className="space-y-1">
        <p className="text-[14px] font-medium text-[var(--foreground)]">
          {title}
        </p>
        <p className="mx-auto max-w-sm text-[12px] leading-relaxed text-[var(--muted-foreground)]">
          {subtitle}
        </p>
      </div>
    </div>
  );
}

/* ----------------------------- footer ----------------------------- */

function WizardFooter({
  phase,
  source,
  selectedCount,
  canImport,
  onCancel,
  // 原代码：onPick,
  // 原代码：onPickChatGpt,
  onImport,
  onDone,
  onRetry,
}: {
  phase: Phase;
  source: ImportSource | null;
  selectedCount: number;
  canImport: boolean;
  onCancel: () => void;
  // 原代码：onPick: () => void;
  // 原代码：onPickChatGpt: () => void;
  onImport: () => void;
  onDone: () => void;
  onRetry: () => void;
}) {
  const { t } = useTranslation();

  if (phase === "intro") {
    return (
      <div className="flex flex-wrap items-center justify-end gap-2">
        <Button variant="ghost" size="sm" onClick={onCancel}>
          {t("Cancel")}
        </Button>
        {/* 原代码（第三方导入按钮，已隐藏）：
        <Button
          variant="primary"
          size="sm"
          icon={<FolderInput className="h-4 w-4" />}
          onClick={onPick}
          disabled={!isFileSystemAccessSupported()}
        >
          {t("Select folder")}
        </Button>
        <Button
          variant="primary"
          size="sm"
          icon={<FileJson className="h-4 w-4" />}
          onClick={onPickChatGpt}
        >
          {t("Select ChatGPT export")}
        </Button>
        */}
      </div>
    );
  }

  if (phase === "select") {
    return (
      <div className="flex items-center justify-between gap-2">
        <span className="text-[12px] text-[var(--muted-foreground)]">
          {source ? SOURCE_LABEL[source] : ""}
        </span>
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={onCancel}>
            {t("Cancel")}
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={onImport}
            disabled={!canImport}
          >
            {selectedCount > 0
              ? t("Import {{count}} conversations", { count: selectedCount })
              : t("Import")}
          </Button>
        </div>
      </div>
    );
  }

  if (phase === "done") {
    return (
      <div className="flex items-center justify-end">
        <Button variant="primary" size="sm" onClick={onDone}>
          {t("Done")}
        </Button>
      </div>
    );
  }

  if (phase === "error") {
    return (
      <div className="flex items-center justify-end gap-2">
        <Button variant="ghost" size="sm" onClick={onCancel}>
          {t("Close")}
        </Button>
        <Button variant="primary" size="sm" onClick={onRetry}>
          {t("Try again")}
        </Button>
      </div>
    );
  }

  // scanning / importing: no actions
  return (
    <div className="flex items-center justify-end">
      <span className="text-[12px] text-[var(--muted-foreground)]">
        {t("Working…")}
      </span>
    </div>
  );
}
