import { Box, Text, useStdout } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { Component, type ReactNode } from 'react'

import { $overlayState, patchOverlayState } from '../app/overlayStore.js'
import { $uiTheme } from '../app/uiStore.js'
import { recordParentLifecycle } from '../lib/parentLog.js'

import { getWidgetApp } from './registry.js'
import type { ActiveWidget, AmbientZone, WidgetApp, WidgetInput } from './types.js'

/**
 * The widget-app host. Core integrates through exactly four touchpoints:
 * launch (slash commands), dispatch (the input pipeline), the MODAL render
 * slot (viewport-level), and the AMBIENT dock (in-flow, above the status
 * bar). Everything else — state shape, keybindings, presentation — belongs
 * to the app.
 */

const isAmbient = (app: WidgetApp<never>) => app.mode === 'ambient'

const withoutApp = (dock: ActiveWidget[], id: string) => dock.filter(active => active.appId !== id)

const dockWith = (dock: ActiveWidget[], entry: ActiveWidget) => [...withoutApp(dock, entry.appId), entry]

/** Launch by id. Returns null on success, a printable error/usage line on
 *  refusal — the caller owns the transcript. Relaunching a DOCKED ambient
 *  app (with no new argument) toggles it out of the dock — ambient apps
 *  capture no input, so the command is their only dismissal. */
export function launchWidget(id: string, arg = ''): null | string {
  const app = getWidgetApp(id)

  if (!app) {
    return `unknown widget app: ${id}`
  }

  if (isAmbient(app)) {
    const dock = $overlayState.get().ambient

    if (dock.some(active => active.appId === id) && !arg.trim()) {
      patchOverlayState({ ambient: withoutApp(dock, id) })

      return null
    }
  }

  const state = app.init(arg)

  if (state === null) {
    return app.usage ?? `usage: /${id}`
  }

  if (isAmbient(app)) {
    patchOverlayState({ ambient: dockWith($overlayState.get().ambient, { appId: id, state }) })
  } else {
    patchOverlayState({ widget: { appId: id, state } })
  }

  return null
}

/** Close the MODAL app. Ambient apps dismiss via their launch toggle, so a
 *  modal's Esc can't collaterally clear the dock. */
export const closeWidget = () => patchOverlayState({ widget: null })

/** Programmatic, TYPED launch — bypasses string parsing. Apps use this to
 *  stack each other (the host swaps the active modal app). */
export function openWidget<S>(app: WidgetApp<S>, state: S): void {
  if (isAmbient(app as WidgetApp<never>)) {
    patchOverlayState({ ambient: dockWith($overlayState.get().ambient, { appId: app.id, state }) })
  } else {
    patchOverlayState({ widget: { appId: app.id, state } })
  }
}

/** Async state delivery: patch the app's state ONLY while it is still active
 *  in its slot — a late fetch resolution can never resurrect a closed app or
 *  clobber a different one. This is how data-backed apps land results
 *  outside the input pipeline (see the weather reference app). */
export function updateWidget<S>(app: WidgetApp<S>, fn: (state: S) => S): void {
  if (isAmbient(app as WidgetApp<never>)) {
    const dock = $overlayState.get().ambient

    if (!dock.some(active => active.appId === app.id)) {
      return
    }

    patchOverlayState({
      ambient: dock.map(active => (active.appId === app.id ? { appId: app.id, state: fn(active.state as S) } : active))
    })

    return
  }

  const active = $overlayState.get().widget

  if (active?.appId !== app.id) {
    return
  }

  patchOverlayState({ widget: { appId: app.id, state: fn(active.state as S) } })
}

/** Feed one keypress to the active MODAL app (ambient apps capture no
 *  input). Returns true when a modal app is active — apps swallow every key
 *  while open. */
export function dispatchWidgetInput(input: WidgetInput): boolean {
  const active = $overlayState.get().widget

  if (!active) {
    return false
  }

  const app = getWidgetApp(active.appId)

  if (!app) {
    closeWidget()

    return true
  }

  const next = app.reduce(active.state as never, input)

  if (next === null) {
    closeWidget()
  } else if (next !== active.state) {
    patchOverlayState({ widget: { appId: active.appId, state: next } })
  }

  return true
}

/** Crash isolation: a widget throwing in render must NEVER take the TUI
 *  down (user widgets are agent-generated code). The boundary swaps the
 *  card for a compact error chip and logs; the app stays registered so a
 *  hot-reloaded fix re-renders on the next state change. */
class WidgetBoundary extends Component<
  { appId: string; children: ReactNode; errorColor: string },
  { message: null | string }
> {
  override state: { message: null | string } = { message: null }

  static getDerivedStateFromError(error: unknown) {
    return { message: error instanceof Error ? error.message : String(error) }
  }

  override componentDidCatch(error: unknown) {
    recordParentLifecycle(
      `widget /${this.props.appId} crashed in render: ${error instanceof Error ? error.message : String(error)}`
    )
  }

  override render() {
    if (this.state.message !== null) {
      return (
        <Text color={this.props.errorColor} wrap="truncate-end">
          ⚠ /{this.props.appId}: {this.state.message}
        </Text>
      )
    }

    return this.props.children
  }
}

const renderApp = (active: ActiveWidget, ctx: { cols: number; rows: number; t: never }) => {
  const app = getWidgetApp(active.appId)

  if (!app) {
    return null
  }

  const t = ctx.t as { color: { error: string } }

  return (
    <WidgetBoundary appId={active.appId} errorColor={t.color.error} key={active.appId}>
      {app.render({ ...ctx, state: active.state as never })}
    </WidgetBoundary>
  )
}

/** Render slot for the MODAL app — viewport-level, so it can anchor
 *  `Overlay` zones and backdrops against the full terminal. */
export function ActiveWidgetSlot(): ReactNode {
  const overlay = useStore($overlayState)
  const t = useStore($uiTheme)
  const { stdout } = useStdout()

  if (!overlay.widget) {
    return null
  }

  return renderApp(overlay.widget, { cols: stdout?.columns ?? 80, rows: stdout?.rows ?? 24, t: t as never })
}

const zoneOf = (active: ActiveWidget): AmbientZone => getWidgetApp(active.appId)?.zone ?? 'dock-bottom'

const useAmbientCtx = () => {
  const t = useStore($uiTheme)
  const { stdout } = useStdout()

  return { cols: stdout?.columns ?? 80, rows: stdout?.rows ?? 24, t: t as never }
}

/** An in-FLOW dock row: reserves real rows in the chrome (never covers
 *  content), right-aligned cards. `dock-top` renders under the top status
 *  bar, `dock-bottom` above the bottom one. */
export function AmbientDock({ placement }: { placement: 'dock-bottom' | 'dock-top' }): ReactNode {
  const overlay = useStore($overlayState)
  const ctx = useAmbientCtx()
  const docked = overlay.ambient.filter(active => zoneOf(active) === placement)

  if (!docked.length) {
    return null
  }

  // paddingRight keeps card borders off the terminal's last column — an
  // exact-edge border char trips pending-wrap and reads as a clipped border.
  return (
    <Box columnGap={1} flexDirection="row" justifyContent="flex-end" paddingRight={2} width="100%">
      {docked.map(active => (
        <Box key={active.appId}>{renderApp(active, ctx)}</Box>
      ))}
    </Box>
  )
}

const FLOAT_ZONES: readonly { align: 'flex-end' | 'flex-start'; anchor: 'bottom' | 'top'; zone: AmbientZone }[] = [
  { align: 'flex-start', anchor: 'top', zone: 'top-left' },
  { align: 'flex-end', anchor: 'top', zone: 'top-right' },
  { align: 'flex-start', anchor: 'bottom', zone: 'bottom-left' },
  { align: 'flex-end', anchor: 'bottom', zone: 'bottom-right' }
]

/** The float layer: corner-anchored widgets overlaying the transcript
 *  margins (absolute, no reserved rows — GUI-corner style). Same-corner
 *  floats stack vertically. Mounted inside the transcript's relative
 *  container so corners track the content area, not the whole screen. */
export function AmbientFloats(): ReactNode {
  const overlay = useStore($overlayState)
  const ctx = useAmbientCtx()

  const corners = FLOAT_ZONES.map(spec => ({
    ...spec,
    apps: overlay.ambient.filter(active => zoneOf(active) === spec.zone)
  })).filter(corner => corner.apps.length)

  if (!corners.length) {
    return null
  }

  return (
    <>
      {corners.map(({ align, anchor, apps, zone }) => (
        <Box
          alignItems={align}
          flexDirection="column"
          key={zone}
          left={0}
          paddingX={2}
          position="absolute"
          rowGap={1}
          width="100%"
          {...(anchor === 'top' ? { top: 0 } : { bottom: 0 })}
        >
          {apps.map(active => (
            <Box key={active.appId}>{renderApp(active, ctx)}</Box>
          ))}
        </Box>
      ))}
    </>
  )
}
