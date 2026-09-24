/**
 * ShipFactory for Hermes Desktop.
 *
 * Disk plugin entrypoint for the released @hermes/plugin-sdk. This file is
 * intentionally plain ESM: no JSX, no build step, and no imports outside the
 * SDK + React runtime allow-list.
 */

import {
  Button,
  Codicon,
  haptic,
  host,
  KEYBINDS_AREA,
  PALETTE_AREA,
  relativeTime,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  Tip,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { useMemo, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'shipfactory'
const PAGE_PATH = '/shipfactory'
// The operator dashboard is the authenticated host for the existing xyflow
// bundle. Hermes Desktop's own backend is intentionally headless.
const BUILDER_URL = 'http://127.0.0.1:9130/shipfactory'
const OVERVIEW_KEY = [ID, 'overview']

let pluginRest = null
let pluginStorage = null
let pluginOs = null

const api = (path, options) => {
  if (!pluginRest) return Promise.reject(new Error('ShipFactory backend is not connected.'))
  return pluginRest(path, options)
}

const optional = (path, fallback) => api(path).catch(() => fallback)

const fetchOverview = () =>
  Promise.all([
    api('/projects'),
    api('/v1/recipes'),
    api('/v1/runs'),
    optional('/waiting', []),
    optional('/seats', []),
    optional('/costs?by=seat&since_days=7', []),
    optional('/status', null)
  ]).then(([projects, recipes, runs, approvals, seats, costs, status]) => ({
    projects: Array.isArray(projects?.projects) ? projects.projects : [],
    runtime: projects?.runtime_config ?? null,
    recipes: Array.isArray(recipes?.recipes) ? recipes.recipes : [],
    runs: Array.isArray(runs?.runs) ? runs.runs : [],
    approvals: Array.isArray(approvals) ? approvals : [],
    seats: Array.isArray(seats) ? seats : [],
    costs: Array.isArray(costs) ? costs : [],
    status
  }))

const text = value => (value == null || value === '' ? '—' : String(value))
const compactId = value => text(value).slice(0, 12)
const stateLabel = value => text(value).replaceAll('_', ' ')
const timestampMs = value => {
  if (value == null || value === '') return null
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return null
    return value < 10_000_000_000 ? value * 1000 : value
  }
  const parsed = Date.parse(String(value))
  return Number.isFinite(parsed) ? parsed : null
}
const timeAgo = value => {
  const parsed = timestampMs(value)
  return parsed == null ? '—' : relativeTime(parsed)
}

function StateTag({ value }) {
  const state = stateLabel(value)
  return jsx('span', {
    className:
      'inline-flex items-center border border-(--ui-stroke-secondary) px-1.5 py-0.5 text-[0.625rem] uppercase tracking-[0.08em] text-(--ui-text-tertiary)',
    children: state
  })
}

function Metric({ label, value }) {
  return jsxs('div', {
    className: 'min-w-28 border border-(--ui-stroke-secondary) px-2.5 py-2',
    children: [
      jsx('div', {
        className: 'text-[0.625rem] uppercase tracking-[0.1em] text-(--ui-text-quaternary)',
        children: label
      }),
      jsx('div', { className: 'mt-1 text-sm font-medium tabular-nums', children: value })
    ]
  })
}

function Section({ title, description, children, meta }) {
  return jsxs('section', {
    className: 'flex min-h-0 flex-col gap-2',
    children: [
      jsxs('div', {
        className: 'flex items-end justify-between gap-3 border-b border-(--ui-stroke-secondary) pb-2',
        children: [
          jsxs('div', {
            children: [
              jsx('h2', { className: 'text-sm font-medium', children: title }),
              description
                ? jsx('p', {
                    className: 'mt-0.5 text-xs text-(--ui-text-tertiary)',
                    children: description
                  })
                : null
            ]
          }),
          meta
            ? jsx('span', {
                className: 'shrink-0 text-[0.6875rem] text-(--ui-text-quaternary)',
                children: meta
              })
            : null
        ]
      }),
      children
    ]
  })
}

function Empty({ children }) {
  return jsx('div', {
    className: 'border border-dashed border-(--ui-stroke-secondary) px-3 py-8 text-center text-xs text-(--ui-text-tertiary)',
    children
  })
}

function Projects({ data }) {
  const ordered = useMemo(
    () => [...data.projects].sort((left, right) => {
      if (left.binding === right.binding) return text(left.name).localeCompare(text(right.name))
      return left.binding === 'bound' ? -1 : 1
    }),
    [data.projects]
  )

  return jsx(Section, {
    title: 'Projects',
    description: 'L0 boundaries: repository policy, adapters, workflows, and Runs.',
    meta: `${ordered.length} visible`,
    children: ordered.length
      ? jsx('div', {
          className: 'divide-y divide-(--ui-stroke-secondary)',
          children: ordered.map(project => {
            const rollup = project.rollup ?? {}
            const workflowCount = Array.isArray(project.recipes?.allowed)
              ? project.recipes.allowed.length
              : 0
            return jsxs('article', {
              className: 'flex items-center gap-3 py-3',
              children: [
                jsx(Codicon, { name: project.binding === 'bound' ? 'repo' : 'circle-slash', size: '0.9rem' }),
                jsxs('div', {
                  className: 'min-w-0 flex-1',
                  children: [
                    jsx('div', { className: 'truncate text-sm font-medium', children: project.name }),
                    jsx('div', {
                      className: 'mt-0.5 truncate text-[0.6875rem] text-(--ui-text-tertiary)',
                      children: `${workflowCount} workflows · ${rollup.active ?? 0} active · ${rollup.waiting ?? 0} waiting`
                    })
                  ]
                }),
                jsx(StateTag, { value: project.binding }),
                project.binding === 'bound'
                  ? jsx(Button, {
                      size: 'sm',
                      onClick: () => void openBuilder(),
                      children: 'New workflow'
                    })
                  : null
              ]
            }, project.id)
          })
        })
      : jsx(Empty, { children: 'No ShipFactory projects are visible.' })
  })
}

function Workflows({ data }) {
  return jsx(Section, {
    title: 'Workflows',
    description: 'L2 immutable GraphRunner recipes. Parallel branches remain inside one graph.',
    meta: `${data.recipes.length} published`,
    children: data.recipes.length
      ? jsx('div', {
          className: 'divide-y divide-(--ui-stroke-secondary)',
          children: data.recipes.map(recipe => {
            const boxes = Array.isArray(recipe.boxes) ? recipe.boxes : []
            const adapters = new Set(boxes.map(box => box.who).filter(who => who && who !== 'human')).size
            const protectedGate = boxes.some(box => box.who === 'human')
            return jsxs('article', {
              className: 'flex items-center gap-3 py-3',
              children: [
                jsx(Codicon, { name: 'type-hierarchy-sub', size: '0.9rem' }),
                jsxs('div', {
                  className: 'min-w-0 flex-1',
                  children: [
                    jsx('div', { className: 'truncate text-sm font-medium', children: recipe.name }),
                    jsx('div', {
                      className: 'mt-0.5 text-[0.6875rem] text-(--ui-text-tertiary)',
                      children: `${boxes.length} steps · ${adapters} adapters · ${protectedGate ? 'human gate' : 'no human gate'}`
                    })
                  ]
                }),
                jsx('span', {
                  className: 'font-mono text-[0.625rem] text-(--ui-text-quaternary)',
                  children: compactId(recipe.hash)
                })
              ]
            }, recipe.name)
          })
        })
      : jsx(Empty, { children: 'No GraphRunner workflows have been published.' })
  })
}

function Runs({ data }) {
  const recent = data.runs.slice(0, 30)
  return jsxs('div', {
    className: 'flex min-h-0 flex-col gap-5',
    children: [
      data.approvals.length
        ? jsx(Section, {
            title: 'Operator approvals',
            description: 'Human gates waiting for an explicit operator decision.',
            meta: `${data.approvals.length} waiting`,
            children: jsx('div', {
              className: 'divide-y divide-(--ui-stroke-secondary)',
              children: data.approvals.slice(0, 8).map(gate =>
                jsxs('article', {
                  className: 'flex items-center gap-3 py-3',
                  children: [
                    jsx(Codicon, { name: 'shield', size: '0.9rem' }),
                    jsxs('div', {
                      className: 'min-w-0 flex-1',
                      children: [
                        jsx('div', {
                          className: 'truncate text-sm font-medium',
                          children: gate.review_story?.headline || gate.step_id
                        }),
                        jsx('div', {
                          className: 'mt-0.5 truncate text-[0.6875rem] text-(--ui-text-tertiary)',
                          children: `${gate.recipe_id}@${gate.recipe_version} · ${compactId(gate.instance_id)}`
                        })
                      ]
                    }),
                    jsx(StateTag, { value: 'waiting' })
                  ]
                }, `${gate.instance_id}:${gate.step_id}:${gate.activation}`)
              )
            })
          })
        : null,
      jsx(Section, {
        title: 'Runs',
        description: 'L3 executions. Every row is a separate run of one immutable workflow.',
        meta: `${recent.length} recent`,
        children: recent.length
          ? jsx('div', {
              className: 'divide-y divide-(--ui-stroke-secondary)',
              children: recent.map(run =>
                jsxs('article', {
                  className: 'flex items-center gap-3 py-3',
                  children: [
                    jsx('span', {
                      className: 'w-28 shrink-0 truncate font-mono text-[0.6875rem]',
                      title: run.id,
                      children: compactId(run.id)
                    }),
                    jsx('span', {
                      className: 'min-w-0 flex-1 truncate text-xs text-(--ui-text-secondary)',
                      children: run.recipe_name
                    }),
                    jsx('span', {
                      className: 'hidden max-w-40 truncate text-[0.6875rem] text-(--ui-text-quaternary) md:block',
                      children: run.project_id || run.board
                    }),
                    jsx(StateTag, { value: run.state }),
                    jsx('span', {
                      className: 'w-16 text-right text-[0.6875rem] text-(--ui-text-quaternary)',
                      children: timeAgo(run.updated_at || run.created_at)
                    })
                  ]
                }, run.id)
              )
            })
          : jsx(Empty, { children: 'No GraphRunner runs have started.' })
      })
    ]
  })
}

function Settings({ data }) {
  const liveSeats = data.seats.filter(seat => !seat.paused)
  return jsxs('div', {
    className: 'flex min-h-0 flex-col gap-5',
    children: [
      jsx(Section, {
        title: 'Adapter seats',
        description: 'Configured execution adapters available to workflows.',
        meta: `${liveSeats.length} live`,
        children: liveSeats.length
          ? jsx('div', {
              className: 'flex flex-wrap gap-2 pt-1',
              children: liveSeats.map(seat =>
                jsxs('span', {
                  className: 'inline-flex items-center gap-1.5 border border-(--ui-stroke-secondary) px-2 py-1 text-[0.6875rem]',
                  children: [
                    jsx(Codicon, { name: 'account', size: '0.75rem' }),
                    `${seat.name} · ${seat.executor}`
                  ]
                }, seat.name)
              )
            })
          : jsx(Empty, { children: 'No live adapter seats are configured.' })
      }),
      jsx(Section, {
        title: 'Seven-day usage',
        description: 'Known token usage grouped by adapter seat.',
        meta: `${data.costs.length} seats`,
        children: data.costs.length
          ? jsx('div', {
              className: 'divide-y divide-(--ui-stroke-secondary)',
              children: data.costs.slice(0, 12).map(row =>
                jsxs('div', {
                  className: 'flex items-center justify-between gap-3 py-2 text-xs',
                  children: [
                    jsx('span', { children: row.seat || row.group || 'unknown' }),
                    jsx('span', {
                      className: 'font-mono tabular-nums text-(--ui-text-tertiary)',
                      children: `${row.tokens_total ?? 0} tokens`
                    })
                  ]
                }, row.seat || row.group || JSON.stringify(row))
              )
            })
          : jsx(Empty, { children: 'No known usage in this window.' })
      })
    ]
  })
}

const TABS = [
  ['projects', 'Projects'],
  ['workflows', 'Workflows'],
  ['runs', 'Runs'],
  ['settings', 'Settings']
]

function ShipFactoryPage() {
  const gateway = useValue(host.state.gateway)
  const queryClient = useQueryClient()
  const [tab, setTabState] = useState(() => pluginStorage?.get('tab', 'projects') ?? 'projects')
  const query = useQuery({
    queryKey: OVERVIEW_KEY,
    queryFn: fetchOverview,
    refetchInterval: 15_000
  })

  const setTab = next => {
    haptic('tap')
    setTabState(next)
    pluginStorage?.set('tab', next)
  }
  const refresh = () => queryClient.invalidateQueries({ queryKey: OVERVIEW_KEY })

  let content = null
  if (query.isLoading) {
    content = jsx(Empty, { children: 'Loading ShipFactory…' })
  } else if (query.error || !query.data) {
    content = jsxs('div', {
      className: 'border border-(--ui-stroke-secondary) p-4 text-sm',
      children: [
        jsx('div', { className: 'font-medium', children: 'ShipFactory backend unavailable' }),
        jsx('p', {
          className: 'mt-1 text-xs text-(--ui-text-tertiary)',
          children: query.error?.message || 'The plugin API did not return an overview.'
        }),
        jsx(Button, { className: 'mt-3', size: 'sm', onClick: refresh, children: 'Retry' })
      ]
    })
  } else if (tab === 'workflows') content = jsx(Workflows, { data: query.data })
  else if (tab === 'runs') content = jsx(Runs, { data: query.data })
  else if (tab === 'settings') content = jsx(Settings, { data: query.data })
  else content = jsx(Projects, { data: query.data })

  const activeRuns = query.data?.runs.filter(run => ['running', 'waiting', 'paused'].includes(run.state)).length ?? 0
  const waiting = query.data?.approvals.length ?? 0

  return jsxs('main', {
    className: 'flex h-full min-h-0 flex-col overflow-hidden',
    children: [
      jsxs('header', {
        className: 'flex flex-wrap items-start justify-between gap-3 border-b border-(--ui-stroke-secondary) px-4 py-3',
        children: [
          jsxs('div', {
            children: [
              jsxs('div', {
                className: 'flex items-center gap-2',
                children: [
                  jsx(Codicon, { name: 'server-process', size: '1rem' }),
                  jsx('h1', { className: 'text-base font-semibold', children: 'ShipFactory' })
                ]
              }),
              jsx('p', {
                className: 'mt-1 text-xs text-(--ui-text-tertiary)',
                children: 'Governed projects, immutable workflows, parallel Runs, and operator gates.'
              })
            ]
          }),
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx(StateTag, { value: gateway === 'open' ? 'gateway ready' : gateway }),
              jsx(Button, { size: 'sm', onClick: refresh, disabled: query.isFetching, children: query.isFetching ? 'Refreshing…' : 'Refresh' }),
              jsx(Button, { size: 'sm', onClick: () => void openBuilder(), children: 'Open xyflow builder' })
            ]
          })
        ]
      }),
      jsxs('div', {
        className: 'flex flex-wrap gap-2 border-b border-(--ui-stroke-secondary) px-4 py-2',
        children: [
          jsx(Metric, { label: 'Projects', value: query.data?.projects.length ?? '—' }),
          jsx(Metric, { label: 'Workflows', value: query.data?.recipes.length ?? '—' }),
          jsx(Metric, { label: 'Active Runs', value: activeRuns }),
          jsx(Metric, { label: 'Approvals', value: waiting })
        ]
      }),
      jsx('nav', {
        className: 'flex gap-1 border-b border-(--ui-stroke-secondary) px-4 py-2',
        'aria-label': 'ShipFactory views',
        children: TABS.map(([id, label]) =>
          jsx('button', {
            type: 'button',
            className:
              'border px-2.5 py-1.5 text-xs transition-colors ' +
              (tab === id
                ? 'border-(--ui-accent) bg-(--chrome-action-hover) text-foreground'
                : 'border-(--ui-stroke-secondary) text-(--ui-text-tertiary) hover:text-foreground'),
            'aria-pressed': tab === id,
            onClick: () => setTab(id),
            children: label
          }, id)
        )
      }),
      jsx('div', { className: 'min-h-0 flex-1 overflow-auto p-4', children: content })
    ]
  })
}

function ShipFactoryChip() {
  const query = useQuery({
    queryKey: OVERVIEW_KEY,
    queryFn: fetchOverview,
    refetchInterval: 30_000
  })
  const active = query.data?.runs.filter(run => ['running', 'waiting', 'paused'].includes(run.state)).length ?? 0
  const waiting = query.data?.approvals.length ?? 0
  if (!query.data || (active === 0 && waiting === 0)) return null

  return jsx(Tip, {
    label: `ShipFactory — ${active} active Runs, ${waiting} approvals`,
    children: jsxs('button', {
      type: 'button',
      className:
        'inline-flex h-full items-center gap-1.5 px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground',
      onClick: () => host.navigate(PAGE_PATH),
      children: [jsx(Codicon, { name: 'server-process', size: '0.7rem' }), `${active}/${waiting}`]
    })
  })
}

function openBuilder() {
  haptic('tap')
  if (!pluginOs) return Promise.resolve(false)
  return pluginOs.openExternal(BUILDER_URL).then(opened => {
    if (!opened) host.notify({ kind: 'warning', message: 'Could not open the ShipFactory xyflow builder.' })
    return opened
  })
}

export default {
  id: ID,
  name: 'ShipFactory',
  defaultEnabled: true,
  register(ctx) {
    pluginRest = ctx.rest
    pluginStorage = ctx.storage
    pluginOs = ctx.os

    const open = () => host.navigate(PAGE_PATH)
    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: PAGE_PATH },
        render: () => jsx(ShipFactoryPage, {})
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        order: 55,
        data: { path: PAGE_PATH, label: 'ShipFactory', codicon: 'server-process' }
      },
      {
        id: 'status',
        area: STATUSBAR_AREAS.right,
        order: 85,
        render: () => jsx(ShipFactoryChip, {})
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'shipfactory.open',
          action: 'shipfactory.open',
          label: 'ShipFactory: Open',
          keywords: ['factory', 'workflow', 'runs', 'approvals'],
          run: open
        }
      },
      {
        id: 'open-builder',
        area: PALETTE_AREA,
        data: {
          id: 'shipfactory.openBuilder',
          label: 'ShipFactory: Open xyflow builder',
          keywords: ['factory', 'workflow', 'graph', 'xyflow'],
          run: () => void openBuilder()
        }
      },
      {
        id: 'open',
        area: KEYBINDS_AREA,
        data: {
          id: 'shipfactory.open',
          label: 'ShipFactory: Open',
          category: 'view',
          defaults: ['mod+alt+f'],
          run: open
        }
      }
    ])
  }
}
