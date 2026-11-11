import { fireEvent, render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import AnswerModeMenu, { PartnerLinkMenu } from '@/components/chat/home/AnswerModeMenu'

/**
 * The personas the tray knows about. Only `cv-auditor` is ever summoned, by
 * slug, so the trigger has a slug-to-label translation to make.
 */
const PERSONAS = [
  { name: 'cv-auditor', display_name: '简历审计师', description: '简历诊断', category: 'work', source: 'user', read_only: false },
  { name: 'plain', display_name: '', description: '无显示名', category: '', source: 'user', read_only: false },
]

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
vi.mock('@/lib/personas-api', () => ({ listPersonas: async () => PERSONAS, touchPersonaUsage: vi.fn() }))
vi.mock('next/link', () => ({
  default: ({ href, children, ...props }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}))

const ada = { partner_id: 'p1', name: 'Ada', emoji: '', color: '', avatar: '' } as never

it('hands the turn to a partner or an agent, and takes it back', () => {
  const onChange = vi.fn()
  const view = render(
    <AnswerModeMenu
      placement="down"
      answerers={{ value: null, onChange, partners: [ada], agents: [{ name: 'Claude Code', kind: 'claude_code' }] }}
    />,
  )
  fireEvent.click(screen.getByRole('button', { name: /Expert/ }))
  fireEvent.click(screen.getByRole('option', { name: /Ada/ }))
  expect(onChange).toHaveBeenLastCalledWith('partner:p1')

  view.rerender(
    <AnswerModeMenu
      placement="down"
      answerers={{ value: 'partner:p1', onChange, partners: [ada], agents: [{ name: 'Claude Code', kind: 'claude_code' }] }}
    />,
  )
  // The trigger names whoever answers now.
  expect(screen.getByRole('button', { name: /Expert.*Ada/ })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /Expert/ }))
  fireEvent.click(screen.getByRole('option', { name: /Claude Code/ }))
  expect(onChange).toHaveBeenLastCalledWith('subagent:Claude Code')
  fireEvent.click(screen.getByRole('button', { name: /Expert/ }))
  fireEvent.click(screen.getByRole('option', { name: /composer\.tray\.deeptutor/ }))
  expect(onChange).toHaveBeenLastCalledWith(null)
})

it('names a summoned persona by its label, not by its slug', async () => {
  // The chosen persona is kept as a slug; the trigger reads 简历审计师.
  render(<AnswerModeMenu placement="down" persona={{ value: 'cv-auditor', onChange: vi.fn() }} />)
  expect(await screen.findByRole('button', { name: /Expert.*简历审计师/ })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /cv-auditor/ })).not.toBeInTheDocument()

  // A persona with no display_name, and one the list no longer has, fall back
  // to the slug rather than rendering an empty value.
  const view = render(<AnswerModeMenu placement="down" persona={{ value: 'plain', onChange: vi.fn() }} />)
  expect(await screen.findByRole('button', { name: /Expert.*plain/ })).toBeInTheDocument()
  view.rerender(
    <AnswerModeMenu placement="down" persona={{ value: 'gone', onChange: vi.fn() }} />,
  )
  expect(await screen.findByRole('button', { name: /Expert.*gone/ })).toBeInTheDocument()
})

it('opens the persona gallery from Summon', () => {
  render(<AnswerModeMenu placement="down" persona={{ value: 'cv-auditor', onChange: vi.fn() }} />)
  fireEvent.click(screen.getByRole('button', { name: /Expert/ }))
  expect(screen.getByRole('link', { name: /Summon/ })).toHaveAttribute('href', '/space/personas')
})

it('keeps asking partners apart from handing over, with the way to manage them', () => {
  const onPartner = vi.fn()
  const onGroup = vi.fn()
  render(
    <PartnerLinkMenu
      placement="up"
      partners={{ options: [ada], selected: null, onSelect: onPartner }}
      groups={{
        options: [{ group_id: 'g1', name: 'Study circle', emoji: '', members: [{ name: 'Ada' }] }] as never,
        selected: null,
        onSelect: onGroup,
      }}
    />,
  )
  fireEvent.click(screen.getByRole('button', { name: /Partners/ }))
  expect(screen.getByRole('group', { name: 'Ask partner' })).toBeInTheDocument()
  expect(screen.getByRole('link', { name: /Create a Partner Group/ })).toHaveAttribute('href', '/partners/groups/new')
  fireEvent.click(screen.getByRole('option', { name: /Study circle/ }))
  expect(onGroup).toHaveBeenCalledWith('g1')
  expect(onPartner).not.toHaveBeenCalled()
})
