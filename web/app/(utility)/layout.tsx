import CalendarRuntime from "@/features/calendar/components/CalendarRuntime";
import UtilitySidebar from "@/components/sidebar/UtilitySidebar";
import AppShell from "@/components/layout/AppShell";
import { LayoutPageBar } from "@/features/ui-layout/slots";
import { CapabilityAccessProvider } from "@/components/access/CapabilityAccessContext";
import CapabilityGate from "@/components/access/CapabilityGate";
import { WorkspaceRuntimeBoundary } from "@/components/workspaces/WorkspaceRuntimeBoundary";
import { Suspense } from "react";

/**
 * Same layer order as the workspace layout (see app/(workspace)/layout.tsx):
 * the shell and its sidebar sit outside the scope boundary so switching
 * workspaces does not remount the conversation list, while the content and
 * the calendars' workspace-scoped runtimes inside it are still reset. The
 * page-level Suspense inside CalendarRuntime keeps a suspended page from
 * unmounting the runtime above it — see that layout for the reasoning.
 */
export default function UtilityLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <CapabilityAccessProvider>
      {/* Utility pages read the workspace's own stores too; a switch remounts them. */}
      <Suspense>
        <AppShell sidebar={<UtilitySidebar />} pageBar={<LayoutPageBar />}>
          <WorkspaceRuntimeBoundary>
            <CalendarRuntime>
              <Suspense
                fallback={
                  <div
                    aria-busy="true"
                    className="h-full bg-[var(--background)]"
                  />
                }
              >
                <CapabilityGate>{children}</CapabilityGate>
              </Suspense>
            </CalendarRuntime>
          </WorkspaceRuntimeBoundary>
        </AppShell>
      </Suspense>
    </CapabilityAccessProvider>
  );
}
