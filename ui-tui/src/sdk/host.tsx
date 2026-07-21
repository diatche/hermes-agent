import { useStdout } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import type { ReactNode } from 'react'

import { $overlayState, patchOverlayState } from '../app/overlayStore.js'
import { $uiTheme } from '../app/uiStore.js'

import { getWidgetApp } from './registry.js'
import type { WidgetApp, WidgetInput } from './types.js'

/**
 * The widget-app host. Core integrates through exactly three touchpoints:
 * launch (slash commands), dispatch (the input pipeline), and the render
 * slot (appLayout). Everything else — state shape, keybindings, placement —
 * belongs to the app.
 */

const slotFor = (app: WidgetApp<never>): 'ambient' | 'widget' => (app.mode === 'ambient' ? 'ambient' : 'widget')

/** Launch by id. Returns null on success, a printable error/usage line on
 *  refusal — the caller owns the transcript. Relaunching an active AMBIENT
 *  app toggles it closed (ambient apps capture no input, so the command is
 *  their only dismissal). */
export function launchWidget(id: string, arg = ''): null | string {
  const app = getWidgetApp(id)

  if (!app) {
    return `unknown widget app: ${id}`
  }

  const slot = slotFor(app)

  if (slot === 'ambient' && $overlayState.get().ambient?.appId === id && !arg.trim()) {
    patchOverlayState({ ambient: null })

    return null
  }

  const state = app.init(arg)

  if (state === null) {
    return app.usage ?? `usage: /${id}`
  }

  patchOverlayState({ [slot]: { appId: id, state } })

  return null
}

/** Close the MODAL app. Ambient apps dismiss via their launch toggle, so a
 *  modal's Esc can't collaterally clear a pinned ambient panel. */
export const closeWidget = () => patchOverlayState({ widget: null })

/** Programmatic, TYPED launch — bypasses string parsing. Apps use this to
 *  stack each other (the host swaps the active app). */
export function openWidget<S>(app: WidgetApp<S>, state: S): void {
  patchOverlayState({ [slotFor(app as WidgetApp<never>)]: { appId: app.id, state } })
}

/** Async state delivery: patch the app's state ONLY while it is still active
 *  in its slot — a late fetch resolution can never resurrect a closed app or
 *  clobber a different one. This is how data-backed apps land results
 *  outside the input pipeline (see the weather reference app). */
export function updateWidget<S>(app: WidgetApp<S>, fn: (state: S) => S): void {
  const slot = slotFor(app as WidgetApp<never>)
  const active = $overlayState.get()[slot]

  if (active?.appId !== app.id) {
    return
  }

  patchOverlayState({ [slot]: { appId: app.id, state: fn(active.state as S) } })
}

/** Feed one keypress to the active app. Returns true when a widget is active
 *  (apps swallow every key while open — the overlay is modal). */
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

/** Render slot for the active apps — viewport-level, so apps can anchor
 *  `Overlay` zones and backdrops against the full terminal. Ambient renders
 *  under modal. */
export function ActiveWidgetSlot(): ReactNode {
  const overlay = useStore($overlayState)
  const t = useStore($uiTheme)
  const { stdout } = useStdout()
  const ctx = { cols: stdout?.columns ?? 80, rows: stdout?.rows ?? 24, t }

  const paint = (active: null | { appId: string; state: unknown }) => {
    const app = active ? getWidgetApp(active.appId) : undefined

    return app ? app.render({ ...ctx, state: active!.state as never }) : null
  }

  return (
    <>
      {paint(overlay.ambient)}
      {paint(overlay.widget)}
    </>
  )
}
