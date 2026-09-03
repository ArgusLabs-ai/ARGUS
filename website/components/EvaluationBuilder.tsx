'use client'

import { useState } from 'react'

export interface Constraint {
  id: string
  field: string
  condition: string
  value: string
}

export interface EvalState {
  goal: string
  constraints: Constraint[]
}

interface Props {
  onEval: (state: EvalState | null) => void
  currentEval: EvalState | null
}

const FIELDS = [
  { name: 'overall_status', label: 'Overall status', type: 'string' },
  { name: 'duration_ms', label: 'Duration (ms)', type: 'number' },
  { name: 'step_count', label: 'Step count', type: 'number' },
  { name: 'first_failure_step', label: 'First failure step', type: 'string' },
]

const STRING_CONDITIONS = ['==', '!=', 'contains']
const NUMBER_CONDITIONS = ['==', '!=', '<=', '>=', '<', '>']

function getConditions(field: string): string[] {
  const f = FIELDS.find((fi) => fi.name === field)
  return f?.type === 'number' ? NUMBER_CONDITIONS : STRING_CONDITIONS
}

function newConstraint(): Constraint {
  return {
    id: Math.random().toString(36).slice(2),
    field: 'overall_status',
    condition: '==',
    value: '',
  }
}

const inputStyle: React.CSSProperties = {
  background: 'var(--bg-elevated)',
  border: '1px solid var(--border-default)',
  borderRadius: '6px',
  color: 'var(--text-primary)',
  fontSize: '12px',
  padding: '6px 10px',
  outline: 'none',
}

const selectStyle: React.CSSProperties = {
  ...inputStyle,
  cursor: 'pointer',
}

export default function EvaluationBuilder({ onEval, currentEval }: Props) {
  const [goal, setGoal] = useState('')
  const [constraints, setConstraints] = useState<Constraint[]>([newConstraint()])
  const [showForm, setShowForm] = useState(true)

  function handleEvaluate() {
    const active = constraints.filter((c) => c.value.trim() !== '')
    onEval({ goal, constraints: active })
    setShowForm(false)
  }

  function handleEdit() {
    setShowForm(true)
  }

  function handleClear() {
    onEval(null)
    setGoal('')
    setConstraints([newConstraint()])
    setShowForm(true)
  }

  function addConstraint() {
    setConstraints((prev) => [...prev, newConstraint()])
  }

  function removeConstraint(id: string) {
    setConstraints((prev) => {
      const next = prev.filter((c) => c.id !== id)
      return next.length === 0 ? [newConstraint()] : next
    })
  }

  function updateConstraint(id: string, patch: Partial<Constraint>) {
    setConstraints((prev) =>
      prev.map((c) => {
        if (c.id !== id) return c
        const updated = { ...c, ...patch }
        if (patch.field) {
          const conds = getConditions(updated.field)
          if (!conds.includes(updated.condition)) {
            updated.condition = conds[0]
          }
        }
        return updated
      })
    )
  }

  // Summary view after evaluating
  if (!showForm && currentEval) {
    const active = currentEval.constraints
    return (
      <div
        className="mb-6 rounded-lg p-5"
        style={{ background: 'var(--bg-surface)', border: '1px solid var(--border-default)' }}
      >
        <div className="flex items-center justify-between mb-4">
          <span
            className="text-[10px] font-semibold uppercase tracking-widest"
            style={{ color: 'var(--text-muted)' }}
          >
            Evaluation <span style={{ opacity: 0.5, fontWeight: 400, textTransform: 'none', letterSpacing: 'normal' }}>(soon)</span>
          </span>
          <div className="flex items-center gap-2">
            <button
              onClick={handleEdit}
              className="text-xs px-2.5 py-1 rounded transition-colors"
              style={{
                color: 'var(--text-secondary)',
                border: '1px solid var(--border-default)',
                background: 'var(--bg-elevated)',
              }}
            >
              Edit
            </button>
            <button
              onClick={handleClear}
              className="text-xs px-2 py-1 rounded transition-colors"
              style={{ color: 'var(--text-muted)' }}
            >
              Clear
            </button>
          </div>
        </div>

        {currentEval.goal.trim() && (
          <div className="mb-3">
            <span className="text-[11px]" style={{ color: 'var(--text-muted)' }}>Goal</span>
            <p className="text-sm font-medium mt-0.5" style={{ color: 'var(--text-primary)' }}>
              {currentEval.goal}
            </p>
          </div>
        )}

        {active.length > 0 ? (
          <div>
            <span className="text-[11px] block mb-2" style={{ color: 'var(--text-muted)' }}>
              Must satisfy
            </span>
            <div className="flex flex-col gap-1.5">
              {active.map((c) => (
                <div key={c.id} className="flex items-center gap-1.5 text-xs font-mono">
                  <span style={{ color: 'var(--text-secondary)' }}>
                    {FIELDS.find((f) => f.name === c.field)?.label ?? c.field}
                  </span>
                  <span style={{ color: 'var(--text-muted)' }}>{c.condition}</span>
                  <span style={{ color: 'var(--text-primary)' }}>{c.value}</span>
                </div>
              ))}
            </div>
          </div>
        ) : (
          <span className="text-xs" style={{ color: 'var(--text-muted)' }}>
            No constraints — goal only
          </span>
        )}

        <div className="mt-3 pt-3" style={{ borderTop: '1px solid var(--border-subtle)' }}>
          <span className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
            Pass / fail badges shown in table below
          </span>
        </div>
      </div>
    )
  }

  // Builder form
  return (
    <div
      className="mb-6 rounded-lg p-5"
      style={{ background: 'var(--bg-surface)', border: '1px solid var(--border-default)' }}
    >
      <div className="flex items-center justify-between mb-5">
        <span
          className="text-[10px] font-semibold uppercase tracking-widest"
          style={{ color: 'var(--text-muted)' }}
        >
          Evaluation
        </span>
        {currentEval && (
          <button
            onClick={handleClear}
            className="text-xs px-2 py-1 rounded transition-colors"
            style={{ color: 'var(--text-muted)' }}
          >
            Clear
          </button>
        )}
      </div>

      {/* Goal */}
      <div className="mb-5">
        <label className="text-xs block mb-2" style={{ color: 'var(--text-muted)' }}>
          Goal
        </label>
        <input
          type="text"
          placeholder="What is the goal of this run?"
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          style={{ ...inputStyle, width: '100%', boxSizing: 'border-box' }}
        />
      </div>

      {/* Constraints */}
      <div className="mb-5">
        <label className="text-xs block mb-2.5" style={{ color: 'var(--text-muted)' }}>
          Constraints
        </label>

        <div className="flex flex-col gap-2">
          {constraints.map((c) => {
            const conditions = getConditions(c.field)
            return (
              <div key={c.id} className="flex items-center gap-2">
                <select
                  value={c.field}
                  onChange={(e) => updateConstraint(c.id, { field: e.target.value })}
                  style={{ ...selectStyle, width: '160px', flexShrink: 0 }}
                >
                  {FIELDS.map((f) => (
                    <option key={f.name} value={f.name}>
                      {f.label}
                    </option>
                  ))}
                </select>

                <select
                  value={c.condition}
                  onChange={(e) => updateConstraint(c.id, { condition: e.target.value })}
                  style={{ ...selectStyle, width: '72px', flexShrink: 0 }}
                >
                  {conditions.map((cond) => (
                    <option key={cond} value={cond}>
                      {cond}
                    </option>
                  ))}
                </select>

                <input
                  type="text"
                  placeholder="value"
                  value={c.value}
                  onChange={(e) => updateConstraint(c.id, { value: e.target.value })}
                  style={{ ...inputStyle, flex: 1, minWidth: 0 }}
                />

                <button
                  onClick={() => removeConstraint(c.id)}
                  className="text-xs px-1 transition-colors"
                  style={{ color: 'var(--text-muted)', flexShrink: 0, lineHeight: 1 }}
                  onMouseEnter={(e) => {
                    (e.currentTarget as HTMLButtonElement).style.color = 'var(--tool)'
                  }}
                  onMouseLeave={(e) => {
                    (e.currentTarget as HTMLButtonElement).style.color = 'var(--text-muted)'
                  }}
                  title="Remove constraint"
                >
                  ×
                </button>
              </div>
            )
          })}
        </div>

        <button
          onClick={addConstraint}
          className="mt-3 text-xs transition-colors"
          style={{ color: 'var(--text-muted)' }}
          onMouseEnter={(e) => {
            (e.currentTarget as HTMLButtonElement).style.color = 'var(--text-secondary)'
          }}
          onMouseLeave={(e) => {
            (e.currentTarget as HTMLButtonElement).style.color = 'var(--text-muted)'
          }}
        >
          + Add constraint
        </button>
      </div>

      <button
        onClick={handleEvaluate}
        className="text-xs px-4 py-2 rounded-lg font-medium transition-all"
        style={{
          background: 'var(--ok-dim)',
          color: 'var(--ok)',
          border: '1px solid color-mix(in srgb, var(--ok) 34%, transparent)',
        }}
        onMouseEnter={(e) => {
          (e.currentTarget as HTMLButtonElement).style.background = 'color-mix(in srgb, var(--ok) 34%, transparent)'
        }}
        onMouseLeave={(e) => {
          (e.currentTarget as HTMLButtonElement).style.background = 'var(--ok-dim)'
        }}
      >
        Evaluate
      </button>
    </div>
  )
}
