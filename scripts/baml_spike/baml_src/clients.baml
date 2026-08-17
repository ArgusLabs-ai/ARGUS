// Clients under test for B1.
//
// The question is whether a BAML client can be pointed at ARGUS's real
// transports. ARGUS has two, and PROJECT.md §3 only describes the second:
//
//   1. BYOK direct  — providers.ADAPTERS[openai|anthropic|google], the path
//                     taken whenever the user has any key configured.
//   2. Hosted proxy — a single Supabase Edge Function at
//                     {SUPABASE_URL}/functions/v1/llm-proxy, authenticated
//                     with a Supabase JWT, NOT an OpenAI key.
//
// Every client below reads its base_url/api_key from the environment so the
// spike harness can retarget them at the local fake proxy without editing
// this file.

// ── Path 2: the hosted Supabase proxy ────────────────────────────────────
//
// openai-generic is the only provider that lets us set base_url freely.
// BAML appends "/chat/completions" to base_url, so pointing base_url at the
// function root means the request lands on
// {SUPABASE_URL}/functions/v1/llm-proxy/chat/completions — a subpath of the
// function, which Supabase still routes to llm-proxy. Whether that actually
// holds is probe 1.
client<llm> ArgusHostedProxy {
  provider openai-generic
  options {
    base_url env.ARGUS_SPIKE_PROXY_BASE_URL
    api_key env.ARGUS_SPIKE_PROXY_TOKEN
    model "gpt-4o-mini"
  }
}

// Same endpoint, no retry policy differences — used to check that a BAML
// retry_policy does not silently multiply the per-call cost of a hot-path
// check (PROJECT.md §4: semantic_checker runs on every passing node).
client<llm> ArgusHostedProxyWithRetry {
  provider openai-generic
  retry_policy SpikeExponential
  options {
    base_url env.ARGUS_SPIKE_PROXY_BASE_URL
    api_key env.ARGUS_SPIKE_PROXY_TOKEN
    model "gpt-4o-mini"
  }
}

// ── Path 1: BYOK direct ──────────────────────────────────────────────────
//
// These mirror providers.DEFAULT_MODELS. They are pointed at the fake
// upstream in the spike; in production they would be the real vendor APIs.
client<llm> ArgusByokOpenAI {
  provider openai-generic
  options {
    base_url env.ARGUS_SPIKE_OPENAI_BASE_URL
    api_key env.ARGUS_SPIKE_OPENAI_KEY
    model "gpt-4o-mini"
  }
}

retry_policy SpikeExponential {
  max_retries 2
  strategy {
    type exponential_backoff
    delay_ms 300
    multiplier 1.5
    max_delay_ms 10000
  }
}
