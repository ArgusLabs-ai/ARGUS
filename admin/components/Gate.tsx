'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { createContext, useContext, useEffect, useState } from 'react'
import type { Session } from '@supabase/supabase-js'
import { supabase, type Role } from '@/lib/supabase'

type Admin = { email: string; role: Role }
const AdminContext = createContext<Admin | null>(null)

export function useAdmin(): Admin {
  const admin = useContext(AdminContext)
  if (!admin) throw new Error('useAdmin outside <Gate>')
  return admin
}

const NAV = [
  { href: '/', label: 'Overview' },
  { href: '/rules', label: 'Rules' },
  { href: '/runs', label: 'Runs' },
  { href: '/team', label: 'Team' },
]

// Sign-in and role check. This only decides what to render: the data itself
// is guarded by row-level security, so a non-admin session reads nothing.
export default function Gate({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<Session | null | undefined>(undefined)
  const [role, setRole] = useState<Role | null | undefined>(undefined)
  const path = usePathname()

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => setSession(data.session))
    const { data } = supabase.auth.onAuthStateChange((_event, s) => setSession(s))
    return () => data.subscription.unsubscribe()
  }, [])

  useEffect(() => {
    if (!session) {
      setRole(session === null ? null : undefined)
      return
    }
    supabase.rpc('admin_role').then(({ data, error }) => setRole(error ? null : (data as Role | null)))
  }, [session])

  if (session === undefined || (session && role === undefined)) {
    return <p className="center muted">Loading…</p>
  }
  if (!session) return <SignIn />
  if (!role) return <NotAdmin email={session.user.email ?? ''} />

  const admin = { email: session.user.email ?? '', role }
  return (
    <AdminContext.Provider value={admin}>
      <div className="shell">
        <header className="top">
          <span className="brand">ARGUS · admin</span>
          <nav>
            {NAV.map((n) => {
              const on = n.href === '/' ? path === '/' : path.startsWith(n.href)
              return (
                <Link key={n.href} href={n.href} className={on ? 'on' : ''}>
                  {n.label}
                </Link>
              )
            })}
          </nav>
          <span className="who">
            <span className={`chip ${role === 'owner' ? 'accent' : ''}`}>{role}</span>
            {admin.email}
            <button className="ghost" onClick={() => supabase.auth.signOut()}>
              Sign out
            </button>
          </span>
        </header>
        {children}
      </div>
    </AdminContext.Provider>
  )
}

function SignIn() {
  const [email, setEmail] = useState('')
  const [state, setState] = useState<'idle' | 'sending' | 'sent' | 'error'>('idle')
  const [message, setMessage] = useState('')

  async function send(e: React.FormEvent) {
    e.preventDefault()
    setState('sending')
    const { error } = await supabase.auth.signInWithOtp({
      email: email.trim(),
      options: { emailRedirectTo: window.location.origin },
    })
    if (error) {
      setState('error')
      setMessage(error.message)
      return
    }
    setState('sent')
  }

  return (
    <main className="center">
      <h1>Argus admin</h1>
      <p className="lead">Sign in with the email the owner added.</p>
      {state === 'sent' ? (
        <p>Check {email} for a sign-in link.</p>
      ) : (
        <form className="row" onSubmit={send}>
          <input
            type="email"
            required
            autoComplete="email"
            placeholder="you@example.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            style={{ flex: 1 }}
          />
          <button disabled={state === 'sending'}>Send link</button>
        </form>
      )}
      {state === 'error' && <p className="err">Could not send the link: {message}</p>}
    </main>
  )
}

function NotAdmin({ email }: { email: string }) {
  return (
    <main className="center">
      <h1>No access yet</h1>
      <p className="lead">
        {email} is signed in but is not an admin. Ask the owner to add this email on the Team page,
        then reload.
      </p>
      <button className="ghost" onClick={() => supabase.auth.signOut()}>
        Sign out
      </button>
    </main>
  )
}
