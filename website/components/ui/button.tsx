import { Slot } from '@radix-ui/react-slot'
import { cva, type VariantProps } from 'class-variance-authority'
import * as React from 'react'

import { cn } from '@/lib/utils'

const buttonVariants = cva(
  // .btn is the spec primitive (globals.css): 32px tall, --r-ctl radius,
  // token-driven colours. Variants map onto the spec's own modifiers.
  'btn',
  {
    variants: {
      variant: {
        default: 'btn-primary',
        outline: 'btn-outline',
        secondary: '',
        ghost: 'btn-ghost',
        destructive: 'btn-danger',
        link: 'lnk border-0 bg-transparent px-0 h-auto',
      },
      size: {
        default: '',
        xs: 'btn-sm',
        sm: 'btn-sm',
        lg: '',
        icon: 'btn-icon',
        'icon-sm': 'btn-icon btn-sm',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'default',
    },
  },
)

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : 'button'
    return (
      <Comp
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        {...props}
      />
    )
  },
)
Button.displayName = 'Button'

export { Button, buttonVariants }
