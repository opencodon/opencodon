import { cn } from '@/lib/utils'

const assetPath = (path: string) => `${import.meta.env.BASE_URL}${path.replace(/^\/+/, '')}`

// Brand badge: the Opencodon mark, identical in light/dark. The asset is a
// single-colour transparent PNG (#89C219), so it sits directly on the surface
// rather than on a tile. Size via className (default size-14).
export function BrandMark({ className, ...props }: React.ComponentProps<'span'>) {
  return (
    <span
      className={cn('inline-flex size-14 shrink-0 items-center justify-center', className)}
      {...props}
    >
      <img alt="" className="size-full object-contain" src={assetPath('opencodon-mark.png')} />
    </span>
  )
}
