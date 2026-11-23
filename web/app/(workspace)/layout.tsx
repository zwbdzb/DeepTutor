import { PageCustomizationProvider } from "@/context/PageCustomizationContext";
import CalendarRuntime from "@/features/calendar/components/CalendarRuntime";
import TaskBoardRuntime from "@/components/tasks/TaskBoardRuntime";
import WorkspaceSidebar from "@/components/sidebar/WorkspaceSidebar";
import AppShell from "@/components/layout/AppShell";
import { LayoutPageBar } from "@/features/ui-layout/slots";
import { CapabilityAccessProvider } from "@/components/access/CapabilityAccessContext";
import CapabilityGate from "@/components/access/CapabilityGate";
import { ChatRuntimeProvider } from "@/features/chat";
import { ReadingProvider } from "@/context/ReadingContext";
import { Suspense } from "react";
import { WorkspaceRuntimeBoundary } from "@/components/workspaces/WorkspaceRuntimeBoundary";

/**
 * The shell of every workspace surface.
 *
 * Five layers, and their order is the whole point:
 *
 *   Suspense            useSearchParams() (the sidebar's session list, the
 *                       scope boundary) needs a boundary above it.
 *   ChatRuntimeProvider above the shell: the sidebar lists conversations from
 *                       every workspace and animates the ones still streaming
 *                       in another one, so this runtime must outlive a scope
 *                       switch. Unmounting it also disconnects every live turn
 *                       socket (see ChatStateAdapter's unmount effect).
 *   AppShell+sidebar    the chrome, outside the scope boundary. Its state —
 *                       the loaded conversation list, collapsed folders, the
 *                       scroll position, the mobile drawer — is the same in
 *                       every workspace. It used to sit inside the keyed
 *                       subtree, so every cross-workspace click remounted it
 *                       and the whole column flashed a skeleton.
 *   Scope boundary      keyed by workspace id: resets content (and the scoped
 *                       runtimes below) on a switch, including a query-only
 *                       one and browser back/forward.
 *   Page Suspense       inside every runtime, on purpose. ChatWorkspace mounts
 *                       its `next/dynamic({ssr:false})` pickers and modals
 *                       unconditionally — an `open` prop only hides them — so
 *                       the first render of a chat page throws all of their lazy
 *                       promises at once. A render-time suspend is not a
 *                       transition, so React answers with the *nearest*
 *                       fallback and unmounts what is under it. Without this
 *                       boundary that fallback is AppShell's, taking the open
 *                       document, the board's stream and the reminder loop down
 *                       with the page. Keep it below the runtimes that must
 *                       survive a page that suspends.
 */
export default function WorkspaceLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <CapabilityAccessProvider>
      <PageCustomizationProvider>
      <Suspense>
        <ChatRuntimeProvider>
          <AppShell sidebar={<WorkspaceSidebar />} pageBar={<LayoutPageBar />}>
            <WorkspaceRuntimeBoundary>
              <CalendarRuntime>
              {/* Inside the boundary: each workspace has its own board. */}
              <TaskBoardRuntime>
                {/* Above the page on purpose: sending the first message
              navigates /chat → /chat/<id>, which remounts the page. The open
              document must not die with it. */}
                <ReadingProvider>
                  {/* Nearest boundary to the page, so a suspended picker takes
                      the page down, never the runtimes above it. */}
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
                </ReadingProvider>
              </TaskBoardRuntime>
              </CalendarRuntime>
            </WorkspaceRuntimeBoundary>
          </AppShell>
        </ChatRuntimeProvider>
      </Suspense>
    </PageCustomizationProvider>
    </CapabilityAccessProvider>
  );
}
