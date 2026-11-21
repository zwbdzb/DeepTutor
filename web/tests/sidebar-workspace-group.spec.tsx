import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import OrganizedSessionList from '@/components/sessions/OrganizedSessionList'
import { reconcileUnread } from '@/lib/session-unread'
import type { SessionSummary } from '@/lib/session-api'
import type { ChatWorkspaceRegistration } from '@/lib/workspaces-api'

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key, i18n: { language: 'en' } }) }))

// The task-board colour is the one state a workspace row can carry that is not
// derived from the session itself, and it is exactly what a "no mark on idle
// rows" gate can swallow by accident. Seeding the real board store costs a
// fetch + EventSource harness (see task-links.spec.tsx); the gate under test is
// the list's own decision, so the colour lookup is the seam worth pinning.
vi.mock('@/lib/task-board-api', async importOriginal => {
  const actual = await importOriginal<typeof import('@/lib/task-board-api')>()
  return {
    ...actual,
    sessionTaskColor: (
      board: Parameters<typeof actual.sessionTaskColor>[0],
      sessionId: string,
      workspaceId: string,
    ) => sessionId === 'task-chat'
      ? '#2563eb'
      : actual.sessionTaskColor(board, sessionId, workspaceId),
  }
})

afterEach(cleanup)

// The folder name carries a digit on purpose: with two rows inside, a heading
// that still printed a tally would read "Algebra 22", which an exact text
// assertion catches while a loose /\d/ match would not.
const workspace: ChatWorkspaceRegistration = {
  workspace_id: 'algebra',
  display_name: 'Algebra 2',
  kind: 'workspace',
  status: 'ready',
  archived: false,
  follows_root: true,
  path: '/algebra',
  error: '',
  created_at: '',
}

const rows: SessionSummary[] = [
  {
    id: 'chat-1', session_id: 'chat-1', title: 'Gaussian elimination',
    created_at: 2, updated_at: 2, message_count: 1, last_message: '',
    content_workspace_id: 'algebra', preferences: {},
  },
  {
    id: 'chat-2', session_id: 'chat-2', title: 'Matrix inverse',
    created_at: 1, updated_at: 1, message_count: 1, last_message: '',
    content_workspace_id: 'algebra', preferences: {},
  },
]

function showWorkspace(options: { liveSessionIds?: ReadonlySet<string>; sessions?: SessionSummary[] } = {}) {
  const view = render(<OrganizedSessionList
    sessions={options.sessions ?? rows}
    workspaces={[workspace]}
    groupByWorkspace
    activeSessionId={null}
    liveSessionIds={options.liveSessionIds}
    onSelect={vi.fn()} onRename={vi.fn()} onDelete={vi.fn()} onOrganize={vi.fn()} />)
  return view.container
}

function workspaceGroup(container: HTMLElement) {
  const group = container.querySelector('[data-group-id="workspace:algebra"]')
  if (!group) throw new Error('the Algebra folder did not render')
  return group as HTMLElement
}

/** A folder heading is its group's only toggle button — conversation rows are `div[role="button"]`. */
function workspaceHeading(container: HTMLElement) {
  return within(workspaceGroup(container)).getByRole('button', { name: 'Algebra 2' })
}

/** A conversation row, found by the title a reader sees. */
function rowOf(title: string) {
  const row = screen.getByText(title).closest('[role="button"]')
  if (!row) throw new Error(`no conversation row for ${title}`)
  return row as HTMLElement
}

/**
 * The status mark `SessionAvatar` draws, and nothing else.
 *
 * The settled mark is a raw 12-unit `<svg viewBox="0 0 12 12">`; every lucide
 * glyph on a row (the always-mounted `⋯` conversation-actions icon) is a
 * 24-unit one. A row therefore still contains an `svg` with the mark column
 * empty, so "the row holds no svg" would fail for the wrong reason — this
 * selector is what isolates the column being tested.
 */
function marksOf(row: HTMLElement) {
  return row.querySelectorAll('svg[viewBox="0 0 12 12"]')
}

it('shows no task count in an open workspace heading', () => {
  const container = showWorkspace()
  // The folder is open: its rows are on screen, so a tally would be a tally of
  // what the reader can already see.
  expect(within(container).getByText('Gaussian elimination')).toBeInTheDocument()
  expect(within(container).getByText('Matrix inverse')).toBeInTheDocument()

  const heading = workspaceHeading(container)
  // Exact: a leftover count span appends row-count digits to the name.
  expect(heading.textContent).toBe('Algebra 2')
  // …and structurally, where the old tally sat: the last element in the
  // heading used to be a `tabular-nums` count span.
  expect(heading.querySelector('.tabular-nums')).toBeNull()
  // The empty "Default workspace" folder below it is a workspace too.
  const fallback = container.querySelector('[data-group-id="workspace:default"]')
  expect(fallback?.querySelector('.tabular-nums')).toBeNull()
})

it('drops the disclosure caret from a workspace heading but keeps the folder foldable', () => {
  const container = showWorkspace()
  const heading = workspaceHeading(container)
  expect(heading.querySelector('svg.lucide-chevron-down')).toBeNull()
  expect(heading).toHaveAttribute('aria-expanded', 'true')

  // Pressing the name is the only fold gesture left, so it has to still work.
  fireEvent.click(heading)
  expect(heading).toHaveAttribute('aria-expanded', 'false')
  expect(screen.queryByText('Gaussian elimination')).toBeNull()
  // Folding does not bring the tally or the caret back on a workspace.
  expect(heading.querySelector('.tabular-nums')).toBeNull()
  expect(heading.querySelector('svg.lucide-chevron-down')).toBeNull()
})

it('keeps the hover actions on the workspace heading', () => {
  const container = showWorkspace()
  const group = workspaceGroup(container)
  // Both actions live beside the heading (a button cannot nest a button) and
  // fade in on hover/focus; being off-screen at rest does not unmount them.
  expect(within(group).getByLabelText('folder.group.newChatIn')).toBeInTheDocument()
  expect(within(group).getByLabelText('folder.group.manage')).toBeInTheDocument()
  // They are siblings of the heading, not children of it: nesting broke both
  // the click target and the drag handle before.
  expect(workspaceHeading(container).querySelector('a, button button')).toBeNull()
})

it('marks only the conversations that have something to report', () => {
  showWorkspace({ liveSessionIds: new Set(['chat-1']) })
  // Running: the live orb is the mark, and the settled ring is cross-faded in
  // the same box, so the column is drawn.
  expect(marksOf(rowOf('Gaussian elimination'))).toHaveLength(1)
  // Idle: nothing to say, so no mark — this is the hollow ring a reader was
  // reading as a row number.
  expect(marksOf(rowOf('Matrix inverse'))).toHaveLength(0)
})

it('marks a finished conversation the reader has not opened yet', () => {
  // Unread is a module-level store and means "left the live set while you were
  // looking elsewhere", so it takes a transition rather than a flag. It is a
  // store update, not a render input, so every write goes through `act`.
  act(() => {
    reconcileUnread(new Set(['chat-2']), null)
    reconcileUnread(new Set(), null)
  })
  try {
    showWorkspace()
    expect(marksOf(rowOf('Matrix inverse'))).toHaveLength(1)
    expect(marksOf(rowOf('Gaussian elimination'))).toHaveLength(0)
  } finally {
    // Opening it is what clears the flag — and what keeps this file's other
    // tests independent of this one.
    act(() => reconcileUnread(new Set(), 'chat-2'))
  }
})

it('marks an idle conversation that is linked to a task on the board', () => {
  // The board colour a linked task paints is state too, so the idle-session
  // gate must not be a mark-flag gate.
  const container = showWorkspace({
    sessions: [{ ...rows[0], id: 'task-chat', session_id: 'task-chat', title: 'Sorted by task' }],
  })
  expect(within(container).getByText('Sorted by task')).toBeInTheDocument()
  expect(marksOf(rowOf('Sorted by task'))).toHaveLength(1)
})

/**
 * The folder's children wrapper — the only element that ever drew a rule down
 * the block's left edge. Rows sit three levels under the group.
 */
function childrenWrapperOf(container: HTMLElement) {
  const row = rowOf('Gaussian elimination')
  const wrapper = row.parentElement?.parentElement
  if (!wrapper) throw new Error('the conversation row is not inside the folder block')
  return wrapper as HTMLElement
}

it('indents a folder\u2019s rows without a rule down their left edge', () => {
  const container = showWorkspace()
  // The nesting signal is the indent; the hairline that ran the height of the
  // panel read as a stray border, not as a tree guide.
  expect(childrenWrapperOf(container).className).not.toContain('border-l')
  expect(childrenWrapperOf(container).className).toContain('ml-1.5')
  expect(workspaceGroup(container).querySelector('.border-l')).toBeNull()
})

it('does not put a tooltip on a conversation title', () => {
  showWorkspace()
  const row = rowOf('Gaussian elimination')
  // The title is fully visible; a bubble repeating it verbatim is noise. The
  // wrapper used to exist for every row, which also gave each title an
  // ``aria-describedby`` pointing at a hidden copy of itself.
  expect(row.querySelector('[role="tooltip"]')).toBeNull()
  expect(screen.getByText('Gaussian elimination')).not.toHaveAttribute('aria-describedby')
})

it('paints a folder heading only under the pointer, never for the current scope', () => {
  const container = showWorkspace()
  const algebra = workspaceHeading(container)
  const defaultFolder = container.querySelector('[data-group-id="workspace:default"]')
  if (!defaultFolder) throw new Error('the Default workspace folder did not render')
  const current = within(defaultFolder as HTMLElement).getByRole('button', {
    name: 'Default workspace',
  })

  for (const heading of [algebra, current]) {
    // At rest a heading is indistinguishable from every other heading: the
    // permanent pill it used to wear on the tab's own scope read as leftover
    // hover and boxed one folder in a card the others did not have.
    expect(heading.className).not.toContain('bg-background/60')
    expect(heading.className).not.toContain('ring-1')
    // The pointer is still the painter, and only the pointer.
    expect(heading.className).toContain('hover:bg-background/40')
  }
  // Dropping the paint must not drop the state: the scope stays announced
  // (jsdom carries no URL scope, so the default folder is the current one).
  expect(current).toHaveAttribute('aria-current', 'page')
  expect(algebra).not.toHaveAttribute('aria-current')
})

it('leaves the other groups as they were: a folded Recent keeps its tally and caret', () => {
  render(<OrganizedSessionList sessions={rows} recents
    activeSessionId={null} onSelect={vi.fn()} onRename={vi.fn()} onDelete={vi.fn()} onOrganize={vi.fn()} />)
  const heading = screen.getByRole('button', { name: 'Recent' })
  // Open: the caret is an affordance of its own here, so it stays.
  expect(heading.querySelector('svg.lucide-chevron-down')).not.toBeNull()
  expect(heading.querySelector('.tabular-nums')).toBeNull()

  fireEvent.click(heading)
  expect(heading).toHaveAttribute('aria-expanded', 'false')
  // Folded: the tally is the only hint of what is hidden, so it stays.
  expect(heading.querySelector('.tabular-nums')?.textContent).toBe('2')
  expect(heading.querySelector('svg.lucide-chevron-down')).not.toBeNull()
})
