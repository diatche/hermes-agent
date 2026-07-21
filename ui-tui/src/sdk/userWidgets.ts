import { readdir } from 'fs/promises'
import { homedir } from 'os'
import { join } from 'path'
import { pathToFileURL } from 'url'

import { Box, Text } from '@hermes/ink'
import * as React from 'react'

import { Dialog, Overlay } from '../components/overlay.js'
import { GridAreas, WidgetGrid } from '../components/widgetGrid.js'
import { recordParentLifecycle } from '../lib/parentLog.js'

import { openWidget, updateWidget } from './host.js'
import { defineWidgetApp, listWidgetApps } from './registry.js'
import { isCtrl } from './types.js'

/**
 * User widget apps — Hermes authors its own TUI widgets, mirroring the
 * Python plugin contract: drop `<name>.mjs` into `$HERMES_HOME/tui-widgets/`,
 * default-export `register(sdk)`, and the app surfaces in `/` completions
 * and dispatch automatically (the registry is the catalog). Plain ESM so the
 * production bundle can import it — no bundler, no JSX; `sdk.h` is
 * React.createElement.
 *
 * Trust model matches `~/.hermes/plugins/`: files under HERMES_HOME execute
 * with the TUI's privileges. Load errors log and skip — a broken widget
 * never takes the TUI down.
 */

/** Everything a user widget may touch, passed INTO its register() — user
 *  files have no resolvable import path to the bundle. */
export const widgetSdk = {
  Box,
  Dialog,
  GridAreas,
  Overlay,
  React,
  Text,
  WidgetGrid,
  defineWidgetApp,
  h: React.createElement,
  isCtrl,
  openWidget,
  updateWidget
} as const

export type WidgetSdk = typeof widgetSdk

const widgetsDir = () => join(process.env.HERMES_HOME?.trim() || join(homedir(), '.hermes'), 'tui-widgets')

export interface UserWidgetLoadResult {
  errors: { file: string; message: string }[]
  loaded: string[]
}

/** Scan + import + register. Cache-busted so `/widgets-reload` picks up
 *  edits without restarting the TUI (the old module stays in memory — its
 *  re-`defineWidgetApp` is last-writer-wins, so the fresh definition shadows
 *  the stale one). */
export async function loadUserWidgets(dir = widgetsDir()): Promise<UserWidgetLoadResult> {
  const result: UserWidgetLoadResult = { errors: [], loaded: [] }

  let files: string[]

  try {
    files = (await readdir(dir)).filter(f => f.endsWith('.mjs')).sort()
  } catch {
    return result // no directory = no user widgets, not an error
  }

  const before = new Set(listWidgetApps().map(app => app.id))

  for (const file of files) {
    try {
      const mod = (await import(`${pathToFileURL(join(dir, file)).href}?t=${Date.now()}`)) as {
        default?: (sdk: WidgetSdk) => void
      }

      if (typeof mod.default !== 'function') {
        throw new Error('default export must be register(sdk)')
      }

      mod.default(widgetSdk)
      result.loaded.push(file)
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)

      result.errors.push({ file, message })
      recordParentLifecycle(`user widget ${file} failed to load: ${message}`)
    }
  }

  const added = listWidgetApps()
    .map(app => app.id)
    .filter(id => !before.has(id))

  if (added.length) {
    recordParentLifecycle(`user widgets registered: ${added.join(', ')}`)
  }

  return result
}
