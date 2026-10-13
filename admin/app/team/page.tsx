'use client'

import { useCallback, useEffect, useState } from 'react'
import { useAdmin } from '@/components/Gate'
import { when } from '@/lib/format'
import { supabase, type Role } from '@/lib/supabase'

type AdminRow = { user_id: string; email: string; role: Role; added_at: string }

export default function Team() {
  const me = useAdmin()
  const [rows, setRows] = useState<AdminRow[] | null>(null)
  const [email, setEmail] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    supabase
      .from('admin_users')
      .select('*')
      .order('role', { ascending: false })
      .then(({ data, error }) => (error ? setNote(error.message) : setRows(data as AdminRow[])))
  }, [])

  useEffect(load, [load])

  async function add(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    const { data, error } = await supabase.rpc('add_maintainer', { maintainer_email: email })
    setBusy(false)
    if (error) return setNote(error.message)
    if (data === 'not_signed_in') {
      return setNote(`${email} has not signed in yet. Ask them to open this panel and request a link first.`)
    }
    setNote(`${email} is now a maintainer.`)
    setEmail('')
    load()
  }

  async function remove(row: AdminRow) {
    if (!window.confirm(`Remove ${row.email}? They lose access immediately.`)) return
    const { error } = await supabase.rpc('remove_maintainer', { maintainer_id: row.user_id })
    if (error) return setNote(error.message)
    setNote(`${row.email} removed.`)
    load()
  }

  const owner = me.role === 'owner'
  return (
    <main>
      <h1>Team</h1>
      <p className="lead">
        The owner adds and removes maintainers. Maintainers can read everything here but cannot change the team or
        delete data.
      </p>

      <div className="panel scroll">
        <table>
          <thead>
            <tr>
              <th>Email</th>
              <th>Role</th>
              <th>Added</th>
              {owner && <th />}
            </tr>
          </thead>
          <tbody>
            {rows?.map((r) => (
              <tr key={r.user_id}>
                <td>{r.email}</td>
                <td>
                  <span className={`chip ${r.role === 'owner' ? 'accent' : ''}`}>{r.role}</span>
                </td>
                <td>{when(r.added_at)}</td>
                {owner && (
                  <td>
                    {r.role === 'maintainer' && (
                      <button className="ghost" onClick={() => remove(r)}>
                        Remove
                      </button>
                    )}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {owner && (
        <>
          <h2>Add a maintainer</h2>
          <p className="muted">They sign in once on this panel first (it will say "No access yet"), then you add them.</p>
          <form className="row" onSubmit={add}>
            <input
              type="email"
              required
              placeholder="maintainer@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
            <button disabled={busy}>Add</button>
          </form>
        </>
      )}
      {note && <p>{note}</p>}
    </main>
  )
}
