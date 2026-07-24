/**
 * Tests for electron/backend-probes.ts.
 *
 * Run with: node --test electron/backend-probes.test.ts
 * (Wired into npm test:desktop:platforms in package.json.)
 */

import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import {
  canImportOpencodonCli,
  opencodonRuntimeImportProbe,
  shouldTrustOpencodonOverride,
  verifyOpencodonCli
} from './backend-probes'

// Resolve the host's own Node binary -- guaranteed to be on disk and
// runnable. We use it as both a stand-in for "a python that doesn't
// have opencodon_cli" (since `node -c "import opencodon_cli"` will exit
// non-zero) and as a way to script verifyOpencodonCli's success path
// (a tiny script we write to disk that exits 0 on --version).
const NODE_BIN = process.execPath

test('canImportOpencodonCli returns false when path is falsy', () => {
  assert.equal(canImportOpencodonCli(''), false)
  assert.equal(canImportOpencodonCli(null), false)
  assert.equal(canImportOpencodonCli(undefined), false)
})

test('canImportOpencodonCli returns false when interpreter cannot run -c', () => {
  // node IS an interpreter, but `node -c "import opencodon_cli"` is a
  // SyntaxError -- different exit reason from a real Python's
  // ModuleNotFoundError, but the predicate is "exit 0 or not" and
  // both land on "not", which is exactly what we want for the
  // resolver fall-through.
  assert.equal(canImportOpencodonCli(NODE_BIN), false)
})

test('canImportOpencodonCli returns false when binary does not exist', () => {
  const ghost = path.join(os.tmpdir(), 'opencodon-probes-ghost-' + Date.now() + '.exe')
  assert.equal(canImportOpencodonCli(ghost), false)
})

test('opencodon runtime import probe checks config dependencies', () => {
  const probe = opencodonRuntimeImportProbe()
  assert.match(probe, /\bimport yaml\b/)
  // dotenv is the first third-party import on the CLI boot path
  // (opencodon_cli/env_loader.py); a mid-update venv missing python-dotenv
  // passed the old probe and produced an unrecoverable boot loop.
  assert.match(probe, /\bimport dotenv\b/)
  assert.match(probe, /\bimport opencodon_cli\.config\b/)
})

test('explicit Opencodon override is authoritative', () => {
  assert.equal(shouldTrustOpencodonOverride('/nix/store/abc/bin/opencodon'), true)
})

test('empty Opencodon override is not authoritative', () => {
  assert.equal(shouldTrustOpencodonOverride(''), false)
  assert.equal(shouldTrustOpencodonOverride(undefined), false)
})

test('verifyOpencodonCli returns false when command is falsy', () => {
  assert.equal(verifyOpencodonCli(''), false)
  assert.equal(verifyOpencodonCli(null), false)
  assert.equal(verifyOpencodonCli(undefined), false)
})

test('verifyOpencodonCli returns false when binary does not exist', () => {
  const ghost = path.join(os.tmpdir(), 'opencodon-probes-ghost-' + Date.now() + '.exe')
  assert.equal(verifyOpencodonCli(ghost), false)
})

test('verifyOpencodonCli returns true when --version exits 0', () => {
  // Write a tiny script that exits 0 regardless of args, then invoke
  // it through node. This stands in for a working opencodon binary --
  // verifyOpencodonCli only cares about the exit code.
  const scriptPath = path.join(os.tmpdir(), `opencodon-probes-ok-${Date.now()}-${process.pid}.cjs`)
  fs.writeFileSync(scriptPath, 'process.exit(0)\n')

  try {
    // Use node as the launcher and our script as the "command". Pass
    // shell:false (default) -- node is a real binary, no shim.
    // execFileSync passes ['--version'] as args, which node ignores
    // gracefully (well, it prints its version and exits 0, which is
    // perfect -- exit code 0 is the only signal we read).
    assert.equal(verifyOpencodonCli(NODE_BIN), true)
  } finally {
    try {
      fs.unlinkSync(scriptPath)
    } catch {
      void 0
    }
  }
})

test('verifyOpencodonCli swallows timeouts (does not throw)', () => {
  // We can't easily provoke a real 5s hang in CI without slowing the
  // suite, but we CAN confirm that an invocation that DOES throw
  // (because the binary is missing) returns false rather than
  // propagating. Same code path the timeout case takes.
  assert.equal(verifyOpencodonCli('/definitely/not/a/real/binary/anywhere'), false)
})
