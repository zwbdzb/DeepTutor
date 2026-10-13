"use client";

import { knowledgeBaseRef } from "@/lib/knowledge-helpers";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  knowledgeBaseFilePath,
  knowledgeBaseFilePreviewTextPath,
  knowledgeBaseRemoteFilePreviewPath,
  knowledgeBaseRemoteFilePreviewTextPath,
  type KnowledgeBaseFile,
} from "@/features/knowledge/api/files";
import type { KnowledgeBase } from "@/lib/knowledge-helpers";
import type { TaskState } from "@/hooks/useKnowledgeProgress";
import type { FilePreviewSource } from "@/components/chat/preview/previewerFor";
import { useCollapsiblePanel } from "@/hooks/useCollapsiblePanel";
import KbDocumentList from "./KbDocumentList";
import KbFilePreview from "./KbFilePreview";
import KbVisualCoverage from "./KbVisualCoverage";

interface KbFilesTabProps {
  kb: KnowledgeBase;
  task?: TaskState;
}

/**
 * Master-detail view for the "Files" tab: list of raw documents on the
 * left, inline preview pane on the right. Both the parent KB list (in
 * `/knowledge-bases`) and this file list can be collapsed to icon-only strips
 * to reclaim horizontal space for the actual preview content.
 */
export default function KbFilesTab({ kb, task }: KbFilesTabProps) {
  const { t } = useTranslation();
  const kbRef = knowledgeBaseRef(kb);
  const remote = kb.metadata?.type === "weknora";
  const [selectedFile, setSelectedFile] = useState<KnowledgeBaseFile | null>(
    null,
  );
  const fileListPanel = useCollapsiblePanel("knowledge-file-list");
  const [showText, setShowText] = useState(false);
  const selectFile = useCallback((file: KnowledgeBaseFile | null) => {
    setSelectedFile(file);
    setShowText(false);
  }, []);

  // Bump refreshKey when the active create/upload task settles so newly
  // indexed files appear automatically.
  const taskExecuting = task?.executing === true;
  const [refreshKey, setRefreshKey] = useState(0);
  useEffect(() => {
    if (!remote && !taskExecuting) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setRefreshKey((n) => n + 1);
    }
  }, [remote, taskExecuting]);

  const previewSource = useMemo<FilePreviewSource | null>(() => {
    if (!selectedFile) return null;
    if (remote && selectedFile.id) {
      const textUrl = knowledgeBaseRemoteFilePreviewTextPath(kbRef, selectedFile.id);
      return {
        filename: showText ? `${selectedFile.name}.txt` : selectedFile.name,
        mimeType: showText ? "text/plain" : selectedFile.mime_type ?? undefined,
        url: showText ? textUrl : knowledgeBaseRemoteFilePreviewPath(kbRef, selectedFile.id),
        officePdfUrl: knowledgeBaseRemoteFilePreviewPath(kbRef, selectedFile.id, "pdf"),
        extractedTextUrl: textUrl,
        size: selectedFile.size,
        id: `${kbRef}/${selectedFile.id}`,
      };
    }
    return {
      filename: selectedFile.name,
      mimeType: selectedFile.mime_type ?? undefined,
      url: knowledgeBaseFilePath(kbRef, selectedFile.name),
      extractedTextUrl: knowledgeBaseFilePreviewTextPath(
        kbRef,
        selectedFile.name,
      ),
      size: selectedFile.size,
      id: `${kbRef}/${selectedFile.name}`,
    };
  }, [kbRef, selectedFile, remote, showText]);

  return (
    <div className="flex h-full min-h-0">
      <KbDocumentList
        kbName={kbRef}
        readOnly={remote}
        refreshKey={refreshKey}
        selectedFile={selectedFile?.id ?? selectedFile?.name ?? null}
        onSelect={selectFile}
        collapsed={fileListPanel.collapsed}
        onToggleCollapsed={fileListPanel.toggle}
      />
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        {selectedFile && !remote && <KbVisualCoverage key={`${kbRef}/${selectedFile.name}`} kbName={kbRef} sourcePath={selectedFile.name} revision={refreshKey} />}
        <KbFilePreview
          key={remote ? `${previewSource?.id}/${showText}` : undefined}
          source={previewSource}
          metaSuffix={remote && selectedFile && <button type="button" className="rounded px-2 py-1 hover:bg-[var(--muted)]" onClick={() => setShowText(value => !value)}>{t(showText ? "Original file" : "Parsed text")}</button>}
          fileListCollapsed={fileListPanel.collapsed}
          onToggleFileList={fileListPanel.toggle}
        />
      </div>
    </div>
  );
}
