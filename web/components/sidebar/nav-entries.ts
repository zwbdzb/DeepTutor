import { CalendarDays } from 'lucide-react'
import { LEARNING_HUB } from '@/lib/learning-routes'
import {
  Blocks,
  Bot,
  GraduationCap,
  HeartHandshake,
  House,
  LayoutGrid,
  ListTodo,
  Settings,
  type LucideIcon,
} from 'lucide-react'

import type { Capability } from '@/lib/capability-routes'
import { PERSONALIZATION_ROOTS } from '@/lib/navigation'

export interface NavEntry {
  href: string
  label: string
  icon: LucideIcon
  tooltipKey?: string
  defaultCollapsed?: boolean
  /** Model capability this feature needs; locked when the user lacks it. */
  requires?: Capability
  /**
   * Learning-account surfaces that may see this entry. Absent means the
   * entry is hidden whenever a learning policy is in effect (#1222).
   */
  surfaces?: readonly string[]
}

/**
 * The workspace features, in the order they ship in.
 *
 * This is the *default* arrangement, not the rendered one — a learner can
 * reorder these and fold the ones they don't use into "More"
 * (``lib/sidebar-layout.ts``). Adding an entry here places it for everyone,
 * including people who have already arranged their sidebar: it arrives next to
 * the neighbour it follows below rather than at the bottom of their list.
 */
export const PRIMARY_NAV: NavEntry[] = [
  {
    href: '/chat',
    label: 'Home',
    icon: House,
    tooltipKey: 'Home tooltip',
    requires: 'llm',
    surfaces: ['chat'],
  },
  {
    href: '/partners',
    label: 'Partners',
    icon: HeartHandshake,
    tooltipKey: 'Partners tooltip',
    requires: 'llm',
  },
  {
    href: LEARNING_HUB,
    label: 'Personalized Learning',
    icon: GraduationCap,
    tooltipKey: 'One tutor, your own way to learn.',
    surfaces: ['reading', 'chat', 'books'],
  },
  {
    href: '/personalization',
    label: 'Experts · Skills · Connectors',
    icon: Blocks,
  },
  { href: '/space', label: 'Learning Space', icon: LayoutGrid, tooltipKey: 'Space tooltip' },
  {
    href: '/calendar', label: 'Calendar', icon: CalendarDays, surfaces: ['chat'],
  },
  {
    href: '/kanban',
    label: 'Task Board',
    icon: ListTodo,
    tooltipKey: 'kanban.intro',
    surfaces: ['chat'],
  },
  {
    href: '/agents',
    label: 'My Agents',
    icon: Bot,
    tooltipKey: 'Agents tooltip',
    defaultCollapsed: true,
  },
]

export const SECONDARY_NAV: NavEntry[] = [{ href: '/settings', label: 'Settings', icon: Settings }]

export const DEFAULT_COLLAPSED_NAV = PRIMARY_NAV.filter(entry => entry.defaultCollapsed).map(
  entry => entry.href
)

export const PRIMARY_NAV_HREFS = PRIMARY_NAV.map(entry => entry.href)

export const NAV_BY_HREF = new Map(
  [...PRIMARY_NAV, ...SECONDARY_NAV].map(entry => [entry.href, entry])
)

/**
 * Account pages that belong to the personalization shelf (see
 * ``PERSONALIZATION_ROOTS``) but have no nav entry of their own: personas,
 * skills, MCP and CLI apps still live under `/space/...` in the URL, and memory
 * lives at `/memory`. Prefix matching must not let another shelf claim them —
 * e.g. "Learning Space" for `/space/personas` — so they fall back to the
 * personalization alias below.
 */
const PERSONALIZATION_ONLY_HREFS = PERSONALIZATION_ROOTS.filter(
  root => root !== '/personalization' && !NAV_BY_HREF.has(root)
)

/**
 * Workspace materials that live at top-level URLs instead of under `/space`
 * (see ``DASHBOARD_GROUPS``): favourites, the knowledge center and the learning
 * notebook. In the classic sidebar the Learning Space entry is the only row for
 * them, so it has to stay lit while the reader is inside one.
 */
const RESOURCE_ONLY_HREFS = ['/notebooks', '/knowledge-bases', '/co-writer']

const under = (pathname: string, href: string) =>
  pathname === href || pathname.startsWith(`${href}/`)

export function isNavActive(pathname: string, href: string) {
  // The personalization entry also stays lit on every account page filed under
  // it — including `/agents`, which additionally lights its own row.
  if (href === '/personalization') {
    return PERSONALIZATION_ROOTS.some(root => under(pathname, root))
  }
  if (PERSONALIZATION_ONLY_HREFS.some(root => under(pathname, root))) return false
  if (href === '/space') {
    return under(pathname, href) || RESOURCE_ONLY_HREFS.some(root => under(pathname, root))
  }
  return under(pathname, href)
}

/**
 * A learning policy hides every feature the account cannot call. Settings
 * stays, because the redacted page (theme, language) is still theirs.
 * ``allowedSurfaces === null`` means no policy: the full sidebar.
 */
export function navVisibleUnderPolicy(
  href: string,
  allowedSurfaces: readonly string[] | null,
): boolean {
  if (allowedSurfaces === null) return true
  if (href === '/settings') return true
  const surfaces = NAV_BY_HREF.get(href)?.surfaces
  if (!surfaces?.length) return false
  return surfaces.some(surface => allowedSurfaces.includes(surface))
}
