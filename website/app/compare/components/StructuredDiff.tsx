'use client'

import { useState } from 'react'
import type { NodeDiff } from '../lib/compare-utils'

function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

function syntaxHighlight(json: string): string {
  return escapeHtml(json).replace(
    /("(\\u[\da-fA-F]{4}|\\[^u]|[^\\&])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/g,
    (match) => {
      let cls = 'json-number'
      if (/^"/.test(match)) {
        cls = /:$/.test(match) ? 'json-key' : 'json-string'
      } else if (/true|false/.test(match)) {
        cls = 'json-boolean'
      } else if (/null/.test(match)) {
        cls = 'json-null'
      }
      return `<span class="${cls}">${match}</span>`
    },
  )
}

function DiffPanel({
  label,
  labelColor,
  output,
  changedKeys,
  highlightColor,
}: {
  label: string
  labelColor: string
  output: Record<string, unknown> | null
  changedKeys: Set<string>
  highlightColor: string
}) {
  const json = output ? JSON.stringify(output, null, 2) : '{}'
  const lines = json.split('\n')

  return (
    <div className="flex-1 min-w-0">
      <div className="flex items-center gap-2 px-3 py-2" style={{ borderBottom: '1px solid var(--border)' }}>
        <span className="text-[12px] font-bold" style={{ color: labelColor }}>{label}</span>
      </div>
      <div className="flex items-start gap-0 px-2 py-2" style={{ borderBottom: '1px solid var(--border)' }}>
        <span className="text-[11px] font-medium px-1.5 py-0.5" style={{ color: 'var(--text-tertiary)' }}>
          {'\u25BC'} Output
        </span>
        <span className="text-[10px] font-medium px-1.5 py-0.5 rounded ml-1" style={{ background: 'var(--card)', color: 'var(--text-tertiary)' }}>
          JSON
        </span>
      </div>
      <div className="overflow-auto max-h-[320px]">
        <table className="w-full text-[11px] font-mono">
          <tbody>
            {lines.map((line, i) => {
              const isChanged = Array.from(changedKeys).some((k) => line.includes(`"${k}"`))
              return (
                <tr key={i} style={{ background: isChanged ? `${highlightColor}08` : 'transparent' }}>
                  <td
                    className="text-right px-2 py-0 select-none shrink-0"
                    style={{ color: 'var(--text-tertiary)', width: '36px', borderRight: '1px solid var(--border)' }}
                  >
                    {i + 1}
                  </td>
                  <td className="px-2 py-0 whitespace-pre" style={{ color: 'var(--text-secondary)' }}>
                    <span
                      dangerouslySetInnerHTML={{ __html: syntaxHighlight(line) }}
                      style={isChanged ? { background: `${highlightColor}12`, borderRadius: '2px', padding: '0 2px' } : undefined}
                    />
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export default function StructuredDiff({ diffs, selectedNode }: { diffs: NodeDiff[]; selectedNode?: string }) {
  const [mode, setMode] = useState<'structured' | 'raw'>('structured')

  const firstDiffNode = diffs.find((d) => d.fieldDiffs.length > 0 || d.statusChanged)
  const activeDiff = (selectedNode ? diffs.find((d) => d.name === selectedNode) : firstDiffNode) ?? diffs[0]

  if (!activeDiff) return null

  const nodeIndex = diffs.indexOf(activeDiff) + 1
  const changedKeys = new Set(activeDiff.fieldDiffs.map((f) => f.field))

  return (
    <div className="card rounded-xl overflow-hidden">
      <div className="flex items-center justify-between px-4 py-3" style={{ borderBottom: '1px solid var(--border)' }}>
        <h3 className="text-[13px] font-bold" style={{ color: 'var(--foreground)' }}>
          Diff View: <span className="font-mono">{activeDiff.name}</span>
          <span className="ml-1 font-normal" style={{ color: 'var(--text-tertiary)' }}>(Node {nodeIndex})</span>
        </h3>
        <div className="flex items-center gap-0 rounded-lg overflow-hidden" style={{ border: '1px solid var(--border)' }}>
          <button
            type="button"
            onClick={() => setMode('structured')}
            className="text-[11px] font-semibold px-3 py-1 transition-colors"
            style={{
              background: mode === 'structured' ? 'var(--iris)' : 'transparent',
              color: mode === 'structured' ? 'var(--on-accent)' : 'var(--text-tertiary)',
            }}
          >
            Structured Diff
          </button>
          <button
            type="button"
            onClick={() => setMode('raw')}
            className="text-[11px] font-semibold px-3 py-1 transition-colors"
            style={{
              background: mode === 'raw' ? 'var(--iris)' : 'transparent',
              color: mode === 'raw' ? 'var(--on-accent)' : 'var(--text-tertiary)',
              borderLeft: '1px solid var(--border)',
            }}
          >
            Raw Diff
          </button>
        </div>
      </div>

      <div className="flex" style={{ minHeight: '200px' }}>
        <DiffPanel
          label="Base Run (Failed)"
          labelColor="var(--tool)"
          output={activeDiff.before?.output_dict ?? null}
          changedKeys={changedKeys}
          highlightColor="var(--tool)"
        />
        <div style={{ width: '1px', background: 'var(--border)' }} />
        <DiffPanel
          label="Replay 1 (Fixed)"
          labelColor="var(--ok)"
          output={activeDiff.after?.output_dict ?? null}
          changedKeys={changedKeys}
          highlightColor="var(--ok)"
        />
      </div>

      <div className="px-4 py-2.5 flex items-center justify-end" style={{ borderTop: '1px solid var(--border)' }}>
        <span className="text-[11px] font-medium flex items-center gap-1 cursor-pointer" style={{ color: 'var(--iris)' }}>
          Open full diff in new tab
          <svg width="10" height="10" viewBox="0 0 10 10" fill="none"><path d="M3 1h6v6M8.5 1.5L4 6" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/></svg>
        </span>
      </div>
    </div>
  )
}
