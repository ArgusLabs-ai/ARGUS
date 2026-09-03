'use client'

import { useState, useEffect, useCallback } from 'react'

interface Team {
  id: string
  name: string
  key: string
}

interface SettingsState {
  linear_api_key_set: boolean
  linear_api_key_masked: string
  linear_team_id: string
  linear_team_name: string
}

export default function SettingsPage() {
  const [settings, setSettings] = useState<SettingsState | null>(null)
  const [apiKey, setApiKey] = useState('')
  const [teams, setTeams] = useState<Team[]>([])
  const [selectedTeamId, setSelectedTeamId] = useState('')
  const [selectedTeamName, setSelectedTeamName] = useState('')
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [status, setStatus] = useState<{ type: 'success' | 'error'; message: string } | null>(null)
  const [editingKey, setEditingKey] = useState(false)

  const loadSettings = useCallback(() => {
    fetch('/api/settings')
      .then(r => r.json())
      .then((data: SettingsState) => {
        setSettings(data)
        setSelectedTeamId(data.linear_team_id || '')
        setSelectedTeamName(data.linear_team_name || '')
      })
      .catch(() => {})
  }, [])

  useEffect(() => {
    loadSettings()
  }, [loadSettings])

  async function handleTestConnection() {
    setTesting(true)
    setStatus(null)

    // If user entered a new key, save it first
    if (apiKey.trim()) {
      try {
        await fetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ linear_api_key: apiKey.trim() }),
        })
      } catch {
        setStatus({ type: 'error', message: 'Failed to save API key' })
        setTesting(false)
        return
      }
    }

    try {
      const res = await fetch('/api/linear/teams')
      const data = await res.json()
      if (data.error) {
        setStatus({ type: 'error', message: data.error })
      } else if (data.teams?.length > 0) {
        setTeams(data.teams)
        setStatus({ type: 'success', message: `Connected — ${data.teams.length} team(s) found` })
        setEditingKey(false)
        setApiKey('')
        loadSettings()
      } else {
        setStatus({ type: 'error', message: 'Connected but no teams found' })
      }
    } catch {
      setStatus({ type: 'error', message: 'Could not reach Linear API' })
    }

    setTesting(false)
  }

  async function handleSaveTeam() {
    if (!selectedTeamId) return
    setSaving(true)
    setStatus(null)

    try {
      const team = teams.find(t => t.id === selectedTeamId)
      await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          linear_team_id: selectedTeamId,
          linear_team_name: team?.name || '',
        }),
      })
      setSelectedTeamName(team?.name || '')
      setStatus({ type: 'success', message: 'Settings saved' })
      loadSettings()
    } catch {
      setStatus({ type: 'error', message: 'Failed to save settings' })
    }

    setSaving(false)
  }

  async function handleDisconnect() {
    setSaving(true)
    setStatus(null)

    try {
      await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          linear_api_key: '',
          linear_team_id: '',
          linear_team_name: '',
        }),
      })
      setTeams([])
      setSelectedTeamId('')
      setSelectedTeamName('')
      setApiKey('')
      setEditingKey(false)
      setStatus({ type: 'success', message: 'Linear disconnected' })
      loadSettings()
    } catch {
      setStatus({ type: 'error', message: 'Failed to update settings' })
    }

    setSaving(false)
  }

  const isConnected = settings?.linear_api_key_set && settings?.linear_team_id

  return (
    <div className="min-h-screen" style={{ background: 'var(--bg-base)' }}>
      <div className="max-w-2xl mx-auto px-6 py-10">
        {/* Page header */}
        <div className="mb-8">
          <h1 className="text-[22px] font-bold tracking-tight" style={{ color: 'var(--text-primary)' }}>
            Settings
          </h1>
          <p className="text-[13px] mt-1" style={{ color: 'var(--text-secondary)' }}>
            Configure integrations and preferences for your ARGUS instance.
          </p>
        </div>

        {/* Linear Integration Card */}
        <div
          className="rounded-xl overflow-hidden"
          style={{ background: 'var(--bg-surface)', border: '1px solid var(--border-default)' }}
        >
          {/* Card header */}
          <div className="px-6 py-4 flex items-center justify-between" style={{ borderBottom: '1px solid var(--border-subtle)' }}>
            <div className="flex items-center gap-3">
              {/* Linear logo */}
              <div className="w-8 h-8 rounded-lg flex items-center justify-center" style={{ background: 'var(--iris)' }}>
                <svg width="16" height="16" viewBox="0 0 100 100" fill="none">
                  <path d="M1.22541 61.5228C0.444112 60.5535 0.444112 59.1375 1.22541 58.1682L41.8316 7.17174C42.613 6.20254 44.029 6.20254 44.8103 7.17174C53.7553 18.2449 53.7553 33.1119 44.8103 44.1852L4.20414 95.1816C3.42284 96.1508 2.00684 96.1508 1.22541 95.1816V61.5228Z" fill="white"/>
                  <path d="M55.1897 44.1852C64.1347 33.1119 64.1347 18.2449 55.1897 7.17174C54.4083 6.20254 55.0769 4.78654 56.3052 4.78654H98.4633C99.5808 4.78654 100.486 5.69207 100.486 6.80947V95.1905C100.486 96.3079 99.5808 97.2135 98.4633 97.2135H56.3052C55.0769 97.2135 54.4083 95.7975 55.1897 94.8282L55.1897 44.1852Z" fill="white"/>
                </svg>
              </div>
              <div>
                <h3 className="text-[14px] font-semibold" style={{ color: 'var(--text-primary)' }}>Linear</h3>
                <p className="text-[11.5px]" style={{ color: 'var(--text-muted)' }}>
                  Create issues from diagnostic reports
                </p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              {isConnected ? (
                <span className="flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-medium"
                  style={{ background: 'var(--ok-dim)', color: 'var(--ok)' }}>
                  <span className="w-1.5 h-1.5 rounded-full" style={{ background: 'var(--ok)' }} />
                  Connected
                </span>
              ) : (
                <span className="flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-medium"
                  style={{ background: 'var(--bg-elevated)', color: 'var(--text-muted)' }}>
                  Not configured
                </span>
              )}
            </div>
          </div>

          {/* Card body */}
          <div className="px-6 py-5 flex flex-col gap-5">
            {/* API Key */}
            <div className="flex flex-col gap-1.5">
              <label className="text-[11px] font-semibold uppercase tracking-wide" style={{ color: 'var(--text-secondary)' }}>
                API Key
              </label>
              {settings?.linear_api_key_set && !editingKey ? (
                <div className="flex items-center gap-2">
                  <div
                    className="flex-1 rounded-lg px-3.5 py-2.5 text-[13px] font-mono"
                    style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border-default)', color: 'var(--text-muted)' }}
                  >
                    {settings.linear_api_key_masked}
                  </div>
                  <button
                    type="button"
                    onClick={() => setEditingKey(true)}
                    className="px-3 py-2.5 rounded-lg text-[12px] font-medium"
                    style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border-default)', color: 'var(--text-secondary)' }}
                  >
                    Change
                  </button>
                </div>
              ) : (
                <div className="flex items-center gap-2">
                  <input
                    type="password"
                    value={apiKey}
                    onChange={e => setApiKey(e.target.value)}
                    placeholder="lin_api_..."
                    className="flex-1 rounded-lg px-3.5 py-2.5 text-[13px] font-mono outline-none"
                    style={{
                      background: 'var(--bg-elevated)',
                      border: '1px solid var(--border-default)',
                      color: 'var(--text-primary)',
                    }}
                  />
                  <button
                    type="button"
                    onClick={handleTestConnection}
                    disabled={testing || (!apiKey.trim() && !settings?.linear_api_key_set)}
                    className="px-3 py-2.5 rounded-lg text-[12px] font-semibold transition-all whitespace-nowrap"
                    style={{
                      background: 'var(--iris)',
                      color: 'var(--on-accent)',
                      opacity: (testing || (!apiKey.trim() && !settings?.linear_api_key_set)) ? 0.5 : 1,
                    }}
                  >
                    {testing ? 'Testing...' : 'Test Connection'}
                  </button>
                </div>
              )}
              <p className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
                Create one at Linear &gt; Settings &gt; API &gt; Personal API keys
              </p>
            </div>

            {/* Team Selector */}
            {(teams.length > 0 || settings?.linear_team_id) && (
              <div className="flex flex-col gap-1.5">
                <label className="text-[11px] font-semibold uppercase tracking-wide" style={{ color: 'var(--text-secondary)' }}>
                  Team
                </label>
                <div className="flex items-center gap-2">
                  {teams.length > 0 ? (
                    <select
                      value={selectedTeamId}
                      onChange={e => setSelectedTeamId(e.target.value)}
                      className="flex-1 rounded-lg px-3.5 py-2.5 text-[13px] outline-none appearance-none cursor-pointer"
                      style={{
                        background: 'var(--bg-elevated)',
                        border: '1px solid var(--border-default)',
                        color: 'var(--text-primary)',
                      }}
                    >
                      <option value="">Select a team...</option>
                      {teams.map(t => (
                        <option key={t.id} value={t.id}>
                          {t.name} ({t.key})
                        </option>
                      ))}
                    </select>
                  ) : (
                    <div
                      className="flex-1 rounded-lg px-3.5 py-2.5 text-[13px]"
                      style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border-default)', color: 'var(--text-primary)' }}
                    >
                      {selectedTeamName || selectedTeamId}
                    </div>
                  )}
                  {teams.length > 0 && (
                    <button
                      type="button"
                      onClick={handleSaveTeam}
                      disabled={!selectedTeamId || saving}
                      className="px-3 py-2.5 rounded-lg text-[12px] font-semibold transition-all"
                      style={{
                        background: 'var(--iris)',
                        color: 'var(--on-accent)',
                        opacity: (!selectedTeamId || saving) ? 0.5 : 1,
                      }}
                    >
                      {saving ? 'Saving...' : 'Save'}
                    </button>
                  )}
                </div>
                <p className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
                  Issues will be created in this team with auto-matched labels.
                </p>
              </div>
            )}

            {/* Status message */}
            {status && (
              <div
                className="rounded-lg px-3.5 py-2.5 text-[12px] font-medium"
                style={{
                  background: status.type === 'success' ? 'var(--ok-dim)' : 'var(--tool-dim)',
                  color: status.type === 'success' ? 'var(--ok)' : 'var(--tool)',
                  border: `1px solid ${status.type === 'success' ? 'color-mix(in srgb, var(--ok) 34%, transparent)' : 'color-mix(in srgb, var(--tool) 34%, transparent)'}`,
                }}
              >
                {status.message}
              </div>
            )}

            {/* Disconnect button */}
            {isConnected && (
              <div className="pt-2" style={{ borderTop: '1px solid var(--border-subtle)' }}>
                <button
                  type="button"
                  onClick={handleDisconnect}
                  disabled={saving}
                  className="px-3 py-2 rounded-lg text-[12px] font-medium transition-all"
                  style={{
                    background: 'transparent',
                    border: '1px solid color-mix(in srgb, var(--tool) 45%, transparent)',
                    color: 'var(--tool)',
                    opacity: saving ? 0.5 : 1,
                  }}
                >
                  Disconnect Linear
                </button>
              </div>
            )}

            {/* How it works */}
            <div
              className="rounded-lg px-4 py-3 mt-1"
              style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border-subtle)' }}
            >
              <p className="text-[10.5px] font-semibold uppercase tracking-wide mb-2" style={{ color: 'var(--text-muted)' }}>
                How it works
              </p>
              <div className="flex flex-col gap-1.5 text-[11.5px]" style={{ color: 'var(--text-secondary)' }}>
                <p>When you send a diagnostic report with &quot;Create Linear issue&quot; enabled:</p>
                <ul className="list-disc list-inside pl-1 flex flex-col gap-0.5">
                  <li>An issue is created in your selected Linear team</li>
                  <li>The report category (Bug, Feature, etc.) is applied as a label</li>
                  <li>Run diagnostics (if included) are added to the issue body</li>
                  <li>Labels are auto-created if they don&apos;t exist yet</li>
                </ul>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
