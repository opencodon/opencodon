/**
 * Client for the science layer's read-only API (`/api/science/*`).
 *
 * The provenance record — which cell produced which artifact version, in which
 * environment, and whether it still reproduces — previously had a UI only in
 * the config dashboard SPA. That left two artifact surfaces with different
 * answers to "what did this session produce". This module brings the record
 * into the session UI so there is one.
 *
 * Every call goes through the host bridge rather than `fetch`, so the same
 * code works under Electron (IPC to the backend) and in the browser (HTTP to
 * the dashboard), and inherits whichever auth scheme that host uses.
 */

import { desktopFsProfile } from '@/lib/desktop-fs'
import { $connection } from '@/store/session'

export type LineageDirection = 'downstream' | 'upstream'

export interface FrameSummary {
  frame_id: string
  title: null | string
  model: null | string
  cwd: null | string
  source: null | string
  profile: null | string
  started_at: null | number
  ended_at: null | number
  /** The session row is gone but its execution record survived retention. */
  session_missing: boolean
  cell_count: number
  failed_cell_count: number
  artifact_count: number
  last_cell_at: null | number
  languages: string[]
}

export interface FramesResponse {
  frames: FrameSummary[]
  total: number
  limit: number
  offset: number
}

export interface FrameEnvironment {
  language: null | string
  env_name: null | string
  kernel_kind: null | string
  cell_count: number
  snapshot: unknown
}

export interface FrameDetail
  extends Omit<FrameSummary, 'artifact_count' | 'cell_count' | 'languages' | 'last_cell_at'> {
  session_ids: string[]
  cell_count: number
  failed_cell_count: number
  artifacts: ArtifactSummary[]
  environments: FrameEnvironment[]
}

export interface CellSummary {
  cell_id: string
  session_id: string
  cell_index: number
  kernel_id: null | string
  kernel_kind: null | string
  language: null | string
  env_name: null | string
  /** Micromamba lock identity; `verified` requires this to still match. */
  env_lock_hash: null | string
  /** Where the kernel ran — local, ssh:…, modal:… */
  kernel_location: null | string
  source: null | string
  stdout: null | string
  stderr: null | string
  exit_status: string
  error_lineno: null | number
  /** "agent" today; the field exists so human-run cells need no migration. */
  origin: string
  user_intervention: null | string
  /** Action label ("Fitting the calibration curve"); null on older cells. */
  description: null | string
  files_written: null | string[]
  files_read: null | string[]
  created_at: null | number
  host_call_count?: number
  version_count?: number
}

export interface FrameCellsResponse {
  frame_id: string
  cells: CellSummary[]
  /** Newest `created_at` seen; pass back as `since` to poll for new cells. */
  cursor: null | number
}

export interface HostCall {
  seq: number
  method: string
  args: unknown
  derivable: boolean
  data_inline: null | string
  data_ref: null | string
  error: null | string
  bytes: number
  created_at: null | number
}

export interface CellDetail extends CellSummary {
  env_snapshot: unknown
  host_calls: HostCall[]
  versions: VersionSummary[]
}

export interface ArtifactSummary {
  artifact_id: string
  frame_id: string
  session_id: null | string
  filename: string
  is_user_upload: boolean
  is_ephemeral: boolean
  latest_version_id: null | string
  latest_version_number: null | number
  latest_content_type: null | string
  latest_size_bytes: null | number
  latest_sha256: null | string
  superseded_by_artifact_id: null | string
  created_at: null | number
}

export interface ArtifactsResponse {
  artifacts: ArtifactSummary[]
  total: number
  limit: number
  offset: number
}

export interface VersionSummary {
  version_id: string
  artifact_id: string
  version_number: number
  session_id: null | string
  content_type: null | string
  size_bytes: null | number
  sha256: null | string
  language: null | string
  is_intermediate: boolean
  producing_cell_id: null | string
  parent_version_id: null | string
  env_snapshot_hash: null | string
  created_at: null | number
}

export interface ArtifactDetail extends ArtifactSummary {
  versions: VersionSummary[]
}

export interface VersionDetail extends VersionSummary {
  filename: null | string
  frame_id: null | string
  producing_cell: CellDetail | null
  depends_on: { version_id: string; reference_name: string }[]
}

export interface LineageEntry extends VersionSummary {
  depth: number
  filename: null | string
}

export interface LineageResponse {
  version_id: string
  direction: LineageDirection
  lineage: LineageEntry[]
}

export interface VersionContent {
  version_id: string
  content_type: null | string
  size_bytes: null | number
  binary: boolean
  truncated: boolean
  text: null | string
}

export interface ReproductionReport {
  claim: string
  reason?: string
  caveats?: string[]
  [key: string]: unknown
}

export interface LiveKernel {
  kernel_id: null | string
  session_id: string
  language: string
  env_name: null | string
  runtime_identity: null | string
  location: string
  workspace: string
  alive: boolean
}

export interface KernelsResponse {
  kernels: LiveKernel[]
  host: {
    cpu_percent?: number
    cpu_count?: number
    memory_percent?: number
    memory_used_bytes?: number
    memory_total_bytes?: number
  }
  /** Set when kernels can't be reported at all (e.g. jupyter not installed). */
  unavailable_reason: null | string
}

export interface ReproductionJob {
  job_id: string
  version_id: string
  state: 'done' | 'error' | 'running'
  report: null | ReproductionReport
}

function bridge() {
  const desktop = window.opencodonDesktop

  if (!desktop) {
    throw new Error('Opencodon host bridge is unavailable')
  }

  return desktop
}

function science<T>(path: string, init?: { method: string }): Promise<T> {
  return bridge().api<T>({
    path: `/api/science${path}`,
    profile: desktopFsProfile(),
    ...(init ?? {})
  })
}

const query = (params: Record<string, null | number | string | undefined>): string => {
  const search = new URLSearchParams()

  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== '') {
      search.set(key, String(value))
    }
  }

  const encoded = search.toString()

  return encoded ? `?${encoded}` : ''
}

export const scienceApi = {
  frames: (params?: { limit?: number; offset?: number }) =>
    science<FramesResponse>(`/frames${query({ limit: params?.limit ?? 50, offset: params?.offset ?? 0 })}`),

  frame: (frameId: string) => science<FrameDetail>(`/frames/${encodeURIComponent(frameId)}`),

  /** `since` is the cursor from a previous call — returns only newer cells. */
  frameCells: (frameId: string, since?: null | number) =>
    science<FrameCellsResponse>(`/frames/${encodeURIComponent(frameId)}/cells${query({ since })}`),

  cell: (cellId: string) => science<CellDetail>(`/cells/${encodeURIComponent(cellId)}`),

  artifacts: (params?: { frameId?: string; limit?: number; offset?: number; search?: string }) =>
    science<ArtifactsResponse>(
      `/artifacts${query({
        frame_id: params?.frameId,
        search: params?.search,
        limit: params?.limit ?? 200,
        offset: params?.offset ?? 0
      })}`
    ),

  artifact: (artifactId: string) => science<ArtifactDetail>(`/artifacts/${encodeURIComponent(artifactId)}`),

  version: (versionId: string) => science<VersionDetail>(`/versions/${encodeURIComponent(versionId)}`),

  lineage: (versionId: string, direction: LineageDirection) =>
    science<LineageResponse>(`/versions/${encodeURIComponent(versionId)}/lineage${query({ direction })}`),

  content: (versionId: string) => science<VersionContent>(`/versions/${encodeURIComponent(versionId)}/content`),

  startReproduction: (versionId: string) =>
    science<ReproductionJob>(`/versions/${encodeURIComponent(versionId)}/reproduce`, { method: 'POST' }),

  reproduction: (jobId: string) => science<ReproductionJob>(`/reproductions/${encodeURIComponent(jobId)}`),

  /** Live kernels in the backend process, plus host CPU/memory. */
  kernels: () => science<KernelsResponse>('/kernels')
}

/**
 * Absolute URLs for raw bytes — used as `<a href>`/`<img src>` rather than
 * fetched, so they must carry auth in the URL itself rather than in a header.
 *
 * Both values come from the connection descriptor rather than from
 * host-injected page globals, so this resolves correctly under Electron (where
 * those globals don't exist) as well as in the browser. On a token backend the
 * query credential authenticates; under the OAuth gate there is no token and
 * the session cookie rides along instead.
 */
export function scienceAssetUrl(path: string): string {
  const connection = $connection.get()

  return `${connection?.baseUrl ?? ''}/api/science${path}${query({
    token: connection?.authMode === 'oauth' ? undefined : connection?.token,
    profile: desktopFsProfile()
  })}`
}

export const versionDownloadUrl = (versionId: string): string =>
  scienceAssetUrl(`/versions/${encodeURIComponent(versionId)}/download`)

export const frameExportUrl = (frameId: string): string =>
  scienceAssetUrl(`/frames/${encodeURIComponent(frameId)}/export`)
