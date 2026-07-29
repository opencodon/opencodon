import { useQuery } from '@tanstack/react-query'

import { getOpencodonConfigRecord } from '@/opencodon'
import { queryClient, writeCache } from '@/lib/query-client'
import type { OpencodonConfigRecord } from '@/types/opencodon'

// One shared cache for the whole profile config record (`GET /api/config`).
// Every settings surface (MCP, model, config) reads and writes through this key
// so a save in one shows in the others, and revisiting a tab paints the cache
// instead of blanking on a fresh fetch.
//
// Distinct from session/hooks/use-opencodon-config.ts, which is side-effecting —
// it pushes personality/cwd/voice/… into the session stores for live chat.
export const OPENCODON_CONFIG_KEY = ['opencodon-config-record'] as const

// staleTime 0 → serve cache instantly, background-revalidate on every mount.
export const useOpencodonConfigRecord = () =>
  useQuery({ queryKey: OPENCODON_CONFIG_KEY, queryFn: getOpencodonConfigRecord, staleTime: 0 })

export const setOpencodonConfigCache = writeCache<OpencodonConfigRecord>(OPENCODON_CONFIG_KEY)

export const invalidateOpencodonConfig = () => queryClient.invalidateQueries({ queryKey: OPENCODON_CONFIG_KEY })
