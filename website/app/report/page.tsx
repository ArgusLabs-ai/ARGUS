'use client'

import { useEffect, useState, useCallback } from 'react'
import { useAuth } from '@/lib/auth'
import { supabase } from '@/lib/supabase'
import SendReportDialog from '@/components/SendReportDialog'

// ── Types ────────────────────────────────────────────────────

type Category = 'feature' | 'bug' | 'failure'
type SortMode = 'most' | 'least'

interface FeedbackPost {
  id: string
  user_id: string
  author_name: string
  author_avatar: string | null
  title: string
  category: Category
  description: string
  vote_count: number
  created_at: string
  user_voted: boolean
}

// ── Constants ────────────────────────────────────────────────

const CATEGORY_COLORS: Record<Category, { bg: string; border: string; text: string }> = {
  feature: { bg: 'var(--iris-dim)', border: 'color-mix(in srgb, var(--iris) 34%, transparent)', text: 'var(--iris)' },
  bug:     { bg: 'var(--tool-dim)',  border: 'color-mix(in srgb, var(--tool) 34%, transparent)',  text: 'var(--tool)' },
  failure: { bg: 'var(--quality-dim)', border: 'color-mix(in srgb, var(--quality) 34%, transparent)', text: 'var(--quality)' },
}

const CATEGORY_LABELS: Record<Category, string> = {
  feature: 'Feature',
  bug: 'Bug',
  failure: 'Failure',
}

const WEBHOOK_URL =
  'https://discord.com/api/webhooks/1505632723539066980/aV5SfeCJ_m6rdxweGQkX0sJUT5mcI95wFU5DVvy1ELTaZQgV34MnhvzwJzsoDZLARNoS'

// ── Helpers ──────────────────────────────────────────────────

function timeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  if (days < 30) return `${days}d ago`
  const months = Math.floor(days / 30)
  return `${months}mo ago`
}

// ── Components ───────────────────────────────────────────────

function Avatar({ name, url, size = 28 }: { name: string; url: string | null; size?: number }) {
  if (url) {
    return (
      <img
        src={url}
        alt={name}
        width={size}
        height={size}
        className="rounded-full shrink-0"
        style={{ width: size, height: size }}
        referrerPolicy="no-referrer"
      />
    )
  }
  const initials = name.split(' ').map(w => w[0]).join('').slice(0, 2).toUpperCase()
  return (
    <div
      className="rounded-full shrink-0 flex items-center justify-center text-[10px] font-bold"
      style={{
        width: size,
        height: size,
        background: 'var(--iris-dim)',
        color: 'var(--iris)',
      }}
    >
      {initials}
    </div>
  )
}

function UpvoteButton({
  count,
  voted,
  onClick,
}: {
  count: number
  voted: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex flex-col items-center gap-0 px-1.5 py-1 rounded-md transition-all hover:opacity-80"
      style={{
        background: voted ? 'var(--iris-dim)' : 'transparent',
        border: `1px solid ${voted ? 'color-mix(in srgb, var(--iris) 45%, transparent)' : 'var(--border-subtle)'}`,
        color: voted ? 'var(--iris)' : 'var(--text-muted)',
        minWidth: 32,
      }}
    >
      <svg width="10" height="10" viewBox="0 0 14 14" fill="none">
        <path
          d="M7 3L11 9H3L7 3Z"
          fill={voted ? 'var(--iris)' : 'none'}
          stroke="currentColor"
          strokeWidth="1.4"
          strokeLinejoin="round"
        />
      </svg>
      <span className="text-[11px] font-semibold leading-none">{count}</span>
    </button>
  )
}

function CategoryBadge({ category }: { category: Category }) {
  const c = CATEGORY_COLORS[category]
  return (
    <span
      className="text-[10.5px] font-semibold px-2 py-0.5 rounded-md shrink-0"
      style={{ background: c.bg, border: `1px solid ${c.border}`, color: c.text }}
    >
      {CATEGORY_LABELS[category]}
    </span>
  )
}

function PostCard({
  post,
  onVote,
}: {
  post: FeedbackPost
  onVote: (postId: string) => void
}) {
  return (
    <div
      className="flex gap-3 px-4 py-3.5 rounded-xl transition-colors"
      style={{ background: 'var(--bg-surface)', border: '1px solid var(--border-subtle)' }}
    >
      <UpvoteButton count={post.vote_count} voted={post.user_voted} onClick={() => onVote(post.id)} />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-1.5">
          <Avatar name={post.author_name} url={post.author_avatar} size={22} />
          <span className="text-[12px] font-medium" style={{ color: 'var(--text-secondary)' }}>
            {post.author_name}
          </span>
          <span className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
            {timeAgo(post.created_at)}
          </span>
          <CategoryBadge category={post.category} />
        </div>
        <h3
          className="text-[14px] font-semibold mb-1 leading-snug"
          style={{ color: 'var(--text-primary)' }}
        >
          {post.title}
        </h3>
        <p
          className="text-[13px] leading-relaxed"
          style={{
            color: 'var(--text-secondary)',
            display: '-webkit-box',
            WebkitLineClamp: 3,
            WebkitBoxOrient: 'vertical',
            overflow: 'hidden',
          }}
        >
          {post.description}
        </p>
      </div>
    </div>
  )
}

// ── Main Page ────────────────────────────────────────────────

export default function FeedbackBoardPage() {
  const { user, signInWithGoogle } = useAuth()

  const [posts, setPosts] = useState<FeedbackPost[]>([])
  const [userVotes, setUserVotes] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState(true)
  const [sort, setSort] = useState<SortMode>('most')
  const [filter, setFilter] = useState<Category | 'all'>('all')
  const [showReportDialog, setShowReportDialog] = useState(false)

  // New post form
  const [showForm, setShowForm] = useState(false)
  const [title, setTitle] = useState('')
  const [category, setCategory] = useState<Category | null>(null)
  const [description, setDescription] = useState('')
  const [submitting, setSubmitting] = useState(false)

  // ── Fetch posts ──────────────────────────────────────────

  const fetchPosts = useCallback(async () => {
    let query = supabase
      .from('feedback_posts')
      .select('*')

    if (filter !== 'all') {
      query = query.eq('category', filter)
    }

    if (sort === 'most') {
      query = query.order('vote_count', { ascending: false }).order('created_at', { ascending: false })
    } else {
      query = query.order('vote_count', { ascending: true }).order('created_at', { ascending: false })
    }

    const { data } = await query
    if (!data) return

    // Fetch user's votes
    let votedIds = new Set<string>()
    if (user) {
      const { data: votes } = await supabase
        .from('feedback_votes')
        .select('post_id')
        .eq('user_id', user.id)
      if (votes) {
        votedIds = new Set(votes.map((v: { post_id: string }) => v.post_id))
      }
    }

    setUserVotes(votedIds)
    setPosts(
      data.map((p: Record<string, unknown>) => ({
        ...p,
        user_voted: votedIds.has(p.id as string),
      })) as FeedbackPost[]
    )
    setLoading(false)
  }, [sort, filter, user])

  useEffect(() => {
    fetchPosts()
  }, [fetchPosts])

  // ── Upvote toggle ────────────────────────────────────────

  async function handleVote(postId: string) {
    if (!user) {
      signInWithGoogle()
      return
    }

    const wasVoted = userVotes.has(postId)

    // Optimistic update
    setPosts(prev =>
      prev.map(p =>
        p.id === postId
          ? { ...p, vote_count: p.vote_count + (wasVoted ? -1 : 1), user_voted: !wasVoted }
          : p
      )
    )
    setUserVotes(prev => {
      const next = new Set(prev)
      if (wasVoted) next.delete(postId)
      else next.add(postId)
      return next
    })

    // DB operation
    if (wasVoted) {
      const { error } = await supabase
        .from('feedback_votes')
        .delete()
        .eq('user_id', user.id)
        .eq('post_id', postId)
      if (error) {
        // Revert
        setPosts(prev =>
          prev.map(p =>
            p.id === postId
              ? { ...p, vote_count: p.vote_count + 1, user_voted: true }
              : p
          )
        )
        setUserVotes(prev => new Set(prev).add(postId))
      }
    } else {
      const { error } = await supabase
        .from('feedback_votes')
        .insert({ user_id: user.id, post_id: postId })
      if (error) {
        // Revert
        setPosts(prev =>
          prev.map(p =>
            p.id === postId
              ? { ...p, vote_count: p.vote_count - 1, user_voted: false }
              : p
          )
        )
        setUserVotes(prev => {
          const next = new Set(prev)
          next.delete(postId)
          return next
        })
      }
    }
  }

  // ── Submit new post ──────────────────────────────────────

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!user || !title.trim() || !category || !description.trim()) return

    setSubmitting(true)

    const authorName = user.user_metadata?.full_name || user.email || 'Anonymous'
    const authorAvatar = user.user_metadata?.avatar_url || null

    const { data, error } = await supabase
      .from('feedback_posts')
      .insert({
        user_id: user.id,
        author_name: authorName,
        author_avatar: authorAvatar,
        title: title.trim(),
        category,
        description: description.trim(),
      })
      .select()
      .single()

    if (!error && data) {
      const newPost: FeedbackPost = { ...(data as FeedbackPost), user_voted: false }
      setPosts(prev => [newPost, ...prev])
      setTitle('')
      setCategory(null)
      setDescription('')
      setShowForm(false)

      // Fire Discord webhook (non-blocking)
      const tagEmoji = { feature: '\u2728', bug: '\uD83D\uDC1B', failure: '\uD83D\uDD25' }
      fetch(WEBHOOK_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          embeds: [{
            title: `${tagEmoji[category]} ${title.trim()}`,
            description: description.trim(),
            color: category === 'feature' ? 0x6366f1 : category === 'bug' ? 0xef4444 : 0xf59e0b,
            fields: [
              { name: 'Category', value: CATEGORY_LABELS[category], inline: true },
              { name: 'Author', value: authorName, inline: true },
            ],
            footer: { text: 'ARGUS Report Board' },
            timestamp: new Date().toISOString(),
          }],
        }),
      }).catch(() => {})
    }

    setSubmitting(false)
  }

  // ── Render ───────────────────────────────────────────────

  const categories: (Category | 'all')[] = ['all', 'feature', 'bug', 'failure']

  return (
    <div className="max-w-3xl mx-auto px-8 py-10 overflow-auto h-full">
      {/* Header */}
      <div className="flex items-start justify-between mb-6">
        <div>
          <h1
            className="text-[22px] font-bold tracking-tight mb-1"
            style={{ color: 'var(--text-primary)' }}
          >
            Report Board
          </h1>
          <p className="text-[13px]" style={{ color: 'var(--text-secondary)' }}>
            Share feedback, request features, and report bugs.
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button
            type="button"
            onClick={() => setShowReportDialog(true)}
            className="px-4 py-2 rounded-lg text-[13px] font-semibold transition-all"
            style={{
              background: 'transparent',
              color: 'var(--tool)',
              border: '1px solid color-mix(in srgb, var(--tool) 45%, transparent)',
            }}
          >
            Send Report
          </button>
          <button
            type="button"
            onClick={() => {
              if (!user) { signInWithGoogle(); return }
              setShowForm(!showForm)
            }}
            className="px-4 py-2 rounded-lg text-[13px] font-semibold transition-all"
            style={{
              background: 'var(--iris)',
              color: 'var(--on-accent)',
            }}
          >
            + New Post
          </button>
        </div>
      </div>

      {/* New Post Form */}
      {showForm && user && (
        <form
          onSubmit={handleSubmit}
          className="rounded-xl p-5 mb-5 flex flex-col gap-4"
          style={{ background: 'var(--bg-surface)', border: '1px solid var(--border-subtle)' }}
        >
          {/* Title */}
          <div className="flex flex-col gap-1.5">
            <label
              className="text-[11px] font-semibold uppercase tracking-wide"
              style={{ color: 'var(--text-secondary)' }}
            >
              Title
            </label>
            <input
              type="text"
              value={title}
              onChange={e => setTitle(e.target.value)}
              placeholder="Short summary"
              maxLength={120}
              className="w-full rounded-lg px-3.5 py-2 text-[13px] outline-none"
              style={{
                background: 'var(--bg-elevated)',
                border: '1px solid var(--border-default)',
                color: 'var(--text-primary)',
              }}
              required
            />
          </div>

          {/* Category */}
          <div className="flex flex-col gap-1.5">
            <label
              className="text-[11px] font-semibold uppercase tracking-wide"
              style={{ color: 'var(--text-secondary)' }}
            >
              Category
            </label>
            <div className="flex gap-2">
              {(['feature', 'bug', 'failure'] as Category[]).map(cat => {
                const active = category === cat
                const c = CATEGORY_COLORS[cat]
                return (
                  <button
                    key={cat}
                    type="button"
                    onClick={() => setCategory(active ? null : cat)}
                    className="px-3 py-1.5 rounded-lg text-[12px] font-medium transition-all"
                    style={{
                      background: active ? c.bg : 'transparent',
                      border: `1px solid ${active ? c.text : 'var(--border-subtle)'}`,
                      color: active ? c.text : 'var(--text-muted)',
                    }}
                  >
                    {CATEGORY_LABELS[cat]}
                  </button>
                )
              })}
            </div>
          </div>

          {/* Description */}
          <div className="flex flex-col gap-1.5">
            <label
              className="text-[11px] font-semibold uppercase tracking-wide"
              style={{ color: 'var(--text-secondary)' }}
            >
              Description
            </label>
            <textarea
              value={description}
              onChange={e => setDescription(e.target.value)}
              placeholder="Describe in detail..."
              rows={4}
              maxLength={2000}
              className="w-full rounded-lg px-3.5 py-2 text-[13px] outline-none resize-none"
              style={{
                background: 'var(--bg-elevated)',
                border: '1px solid var(--border-default)',
                color: 'var(--text-primary)',
              }}
              required
            />
            <p className="text-[11px] text-right" style={{ color: 'var(--text-muted)' }}>
              {description.length}/2000
            </p>
          </div>

          {/* Actions */}
          <div className="flex items-center gap-2">
            <button
              type="submit"
              disabled={!title.trim() || !category || !description.trim() || submitting}
              className="px-4 py-2 rounded-lg text-[13px] font-semibold transition-all"
              style={{
                background: 'var(--iris)',
                color: 'var(--on-accent)',
                opacity: (!title.trim() || !category || !description.trim() || submitting) ? 0.5 : 1,
              }}
            >
              {submitting ? 'Posting...' : 'Post'}
            </button>
            <button
              type="button"
              onClick={() => setShowForm(false)}
              className="px-4 py-2 rounded-lg text-[13px] font-medium"
              style={{ color: 'var(--text-muted)' }}
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {/* Controls bar */}
      <div className="flex items-center gap-3 mb-4 flex-wrap">
        {/* Sort */}
        <div className="flex rounded-lg overflow-hidden" style={{ border: '1px solid var(--border-subtle)' }}>
          {(['most', 'least'] as SortMode[]).map(s => (
            <button
              key={s}
              type="button"
              onClick={() => setSort(s)}
              className="px-3 py-1.5 text-[12px] font-medium transition-all"
              style={{
                background: sort === s ? 'var(--iris-dim)' : 'transparent',
                color: sort === s ? 'var(--iris)' : 'var(--text-muted)',
              }}
            >
              {s === 'most' ? 'Most upvoted' : 'Least upvoted'}
            </button>
          ))}
        </div>

        <div className="w-px h-5" style={{ background: 'var(--border-subtle)' }} />

        {/* Category filter */}
        <div className="flex gap-1.5">
          {categories.map(cat => {
            const active = filter === cat
            const isAll = cat === 'all'
            const c = isAll ? null : CATEGORY_COLORS[cat as Category]
            return (
              <button
                key={cat}
                type="button"
                onClick={() => setFilter(cat)}
                className="px-2.5 py-1 rounded-md text-[11px] font-medium transition-all"
                style={{
                  background: active
                    ? (c ? c.bg : 'var(--fill-subtle)')
                    : 'transparent',
                  border: `1px solid ${active
                    ? (c ? c.border : 'var(--border-default)')
                    : 'transparent'}`,
                  color: active
                    ? (c ? c.text : 'var(--text-primary)')
                    : 'var(--text-muted)',
                }}
              >
                {isAll ? 'All' : CATEGORY_LABELS[cat as Category]}
              </button>
            )
          })}
        </div>
      </div>

      {/* Posts list */}
      {loading ? (
        <div className="text-center py-16 text-[13px]" style={{ color: 'var(--text-muted)' }}>
          Loading...
        </div>
      ) : posts.length === 0 ? (
        <div
          className="text-center py-16 rounded-xl"
          style={{ background: 'var(--bg-surface)', border: '1px solid var(--border-subtle)' }}
        >
          <p className="text-[14px] font-medium mb-1" style={{ color: 'var(--text-secondary)' }}>
            No posts yet
          </p>
          <p className="text-[12px]" style={{ color: 'var(--text-muted)' }}>
            Be the first to share feedback!
          </p>
        </div>
      ) : (
        <div className="flex flex-col gap-2.5">
          {posts.map(post => (
            <PostCard key={post.id} post={post} onVote={handleVote} />
          ))}
        </div>
      )}

      <SendReportDialog open={showReportDialog} onClose={() => setShowReportDialog(false)} />
    </div>
  )
}
