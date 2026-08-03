import type { ComponentProps } from 'react'

import { cn } from '../lib/utils'

const assetPath = (path: string) => `${import.meta.env.BASE_URL}${path.replace(/^\/+/, '')}`

interface LoaderProps extends Omit<ComponentProps<'div'>, 'children'> {
  label?: string
  /** @deprecated Ignored — kept so existing call sites keep type-checking. */
  pathSteps?: number
  /** @deprecated Ignored — kept so existing call sites keep type-checking. */
  strokeScale?: number
}

/**
 * The Opencodon loading indicator: the brand mark, rotating. Mirrors
 * apps/client's Loader; the `.brand-spin` keyframes come from the desktop
 * stylesheet this app imports.
 */
export function Loader({
  className,
  label = 'Loading',
  pathSteps: _pathSteps,
  role = 'status',
  strokeScale: _strokeScale,
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
