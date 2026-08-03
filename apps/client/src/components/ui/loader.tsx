import type { ComponentProps } from 'react'

import { cn } from '@/lib/utils'

const assetPath = (path: string) => `${import.meta.env.BASE_URL}${path.replace(/^\/+/, '')}`

/**
 * Retained for API compatibility — `LoaderType` is re-exported from the public
 * SDK (`sdk/index.ts`), so the union must keep existing. The brand loader is a
 * single mark now, so the value is accepted and ignored.
 *
 * @deprecated The loader no longer varies by curve; pass nothing.
 */
export const LOADER_TYPES = [
  'original-thinking',
  'thinking-five',
  'thinking-nine',
  'rose-orbit',
  'rose-curve',
  'rose-two',
  'rose-three',
  'rose-four',
  'lissajous-drift',
  'lemniscate-bloom',
  'hypotrochoid-loop',
  'three-petal-spiral',
  'four-petal-spiral',
  'five-petal-spiral',
  'six-petal-spiral',
  'butterfly-phase',
  'cardioid-glow',
  'cardioid-heart',
  'heart-wave',
  'spiral-search',
  'fourier-flow'
] as const

export type LoaderType = (typeof LOADER_TYPES)[number]

interface LoaderProps extends Omit<ComponentProps<'div'>, 'children'> {
  label?: string
  /** @deprecated Ignored — kept so existing call sites keep type-checking. */
  pathSteps?: number
  /** @deprecated Ignored — kept so existing call sites keep type-checking. */
  strokeScale?: number
  /** @deprecated Ignored — the loader is always the brand mark. */
  type?: LoaderType
}

/**
 * The Opencodon loading indicator: the brand mark, rotating.
 *
 * The mark is a single-colour PNG, so it carries bio-lime regardless of theme —
 * it does not inherit `currentColor`. On light surfaces the lime sits at 2.14:1,
 * which is fine for a brand mark but faint as a status cue; pair it with a text
 * label where the loading state actually needs to be read.
 */
export function Loader({
  className,
  label = 'Loading',
  pathSteps: _pathSteps,
  role = 'status',
  strokeScale: _strokeScale,
  type: _type,
  ...props
}: LoaderProps) {
  return (
    <div
      {...props}
      aria-label={props['aria-label'] ?? label}
      className={cn('inline-grid size-10 place-items-center', className)}
      role={role}
    >
      <img
        alt=""
        aria-hidden="true"
        className="brand-spin size-full object-contain"
        src={assetPath('opencodon-mark.png')}
      />
    </div>
  )
}
