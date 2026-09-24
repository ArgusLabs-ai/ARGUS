# Test cases for the pivot branch (`pivot/fat-traces`)

How to test the fat-trace architecture the way real teams will use it. The
local suites (`pivot_eval/`, `enterprise_stress/`, `merge_readiness/`) are
gitignored. This file describes what they cover, so you can rebuild any case.

## 1. Always run first (tracked)

```bash
pip install -e ".[dev]"
pytest tests/ -q                                  # 1146 pass
pytest tests/test_silent_failure_matrix.py tests/test_shipped_shapes_matrix.py -q
```

Run both matrices before and after any change to `inspector.py`,
`contextual.py`, `recorder.py`, `session.py` or `semantic_checker.py`.

## 2. The rules for writing a pivot test

1. **Pivot path only.** Use `ArgusRecorder(...).attach(app)` and monkeypatch
   `argus.patcher.patch_graph` to raise.
2. **Every fault test names the node it blames.** "The run failed" is not
   enough: a wrong-node blame is a bug.
3. **About half the tests should be healthy runs that must grade clean.** A
   rule that over-fires is as bad as one that misses.
4. **Healthy runs must include traps**: legitimate outcomes that look like
   failures (see §4).
5. **Known gaps and defects use `xfail(strict=True)`** with an ID, so a fix
   turns red and has to be un-marked on record.
6. **Judge stubbed by default**
   (`monkeypatch.setattr("argus.semantic_checker.check_semantic_coherence", fake)`).
   Live-LLM runs are opt-in, and each is repeated at least 3× to expose flakiness.

## 3. Pipelines to replicate (real-team shapes)

| Pipeline | Shape that matters |
|---|---|
| Insurance claims | LLM parse → policy tool → `Send` fan-out pricing → `operator.add` → fraud `Command` route → adjudicate → payments tool → letter → email tool |
| Text-to-SQL analytics | catalog tool → SQL writer ⟲ linter loop → warehouse tool → chart → summary; empty result is a valid answer |
| SDR outbound | enrichment tool → score → conditional disqualify → personalize → compliance → CRM tool → send tool |
| Support ReAct agent | `create_react_agent`, real `@tool`s, scripted model (lookup → refund → reply) |
| KYC / AML | OCR tool → identity check → `Send` sanctions screens → risk → decide → router → core-banking tool |
| Older suites | support desk, AP invoices, due-diligence subgraphs, PR-review bot, order fulfilment (`.batch` / `ainvoke` / `stream`), supervisor multi-agent, RAG |

## 4. Scenario checklist per pipeline

**Healthy + traps (must be clean):** happy path; a correct *denial*,
*rejection*, *disqualification* or *manual review*; an empty result that *is*
the answer ("no refunds in March", `hits: []` on a sanctions screen); customer
text quoted back ("you said *I can't* before Q3"); a short policy decline ("I'm
sorry, but I can't refund this, it's outside 30 days"); a loop that retries and
self-corrects.

**Rule-visible faults (must fail and blame the origin):** node returns `{}`;
`Command(update={})`; a tool 4xx/5xx payload stored as data; a tool that raised
but was swallowed (in the **first and last** fan-out worker); a node that drops
or blanks a field a later node declares it reads; a bracketed template
(`[Your Name]`); a refusal as the answer; a payment status of `failed` or
`declined`; an empty final agent reply; a crash downstream of a no-op (the
headline must name the no-op, not the crash site).

**Semantic faults (track them; the rules do not see these today):**

| ID | Class | Example |
|---|---|---|
| G1 | wrong ID in a side-effecting tool call | payout to `CLM-7718` for claim `CLM-7781`; refund to order `A-1002` |
| G2 | decision contradicts evidence | approve despite a sanctions hit; risk tier `low` with score 92; refund > order total |
| G3 | wrong arithmetic / misstated number | ignores the deductible; answer states 10× the SQL total |
| G4 | wrong entity in prose | email congratulates `Initech`; lead is `Globex` |
| G5 | claims an action no tool performed | "I've refunded $49.99" with no `issue_refund` call |
| G6 | misreports a tool / upstream value | fraud tool says 0.93, node writes 0.12 |
| G7 | unsupported value | loss date not in the claim text; invented "340% growth" |
| G8 | valid but wrong intent | SQL for 2023 when 2024 was asked |
| G9 | cross-field inconsistency | letter amount ≠ decision amount |

## 5. Judge contract (run on every scenario with stubbed judges)

1. An always-FAIL judge and an always-PASS judge give **exactly** the judge-off
   verdict and blamed set.
2. The judge is asked only about a passing step that carries a warning-level
   signature, never about clean steps or hard fails.
3. Pinned design question: a soft flag the judge *confirms* does not gate
   (`{"answer": "Refund issued. Lorem ipsum…"}` + a judge that says "fail" → CI green).

## 6. Defects found by the real-team suite (pinned in `pivot_eval/`)

| ID | Type | What | Where | Status |
|---|---|---|---|---|
| E1 | false positive | `decision.status: "denied"` (a node's own business outcome) is a critical `error_response` | `inspector.py` Rule 2d ran on node outputs, not just tool payloads | **fixed**: `own_output=True`, `tests/test_own_verdict_vs_tool_response.py` |
| E2 | false positive | `hits: []` (a clean sanctions screen) is a critical `empty_result`; `allow_empty` cannot turn it off. More visible since E4. | `inspector._empty_result_severity` | **fixed**: `allow_empty` softens empty retrieval on the writer node's tools, `tests/test_allow_empty_tool_hits.py` (#129) |
| E3 | false positive | a short legitimate policy decline is critical `BA-004`, so the reviewer never sees it | `anomaly_detector._check_generic_response` whole-answer promotion | [#130](https://github.com/ArgusLabs-ai/ARGUS/issues/130) |
| E4 | **miss** | parallel `Send` workers were graded as retries, so a swallowed tool error in any worker but the last was hidden ($0 line item, CI green) | `session._apply_loop_retries` | **fixed**: siblings share `NodeEvent.superstep`, `tests/test_fanout_siblings.py` |
| E4b | **miss** | a sequential loop that appends to a list still hides a failed iteration (pagination loses page 1, CI green) | same function; a plain "never retry a reducer write" would break ReAct recovery | [#131](https://github.com/ArgusLabs-ai/ARGUS/issues/131) |
| E5 | miss | `[Your Name]`, `[TOPIC]`, `[Claimant Name]` inside prose raise nothing | `signatures.json` PH-014 is whole-value only | [#132](https://github.com/ArgusLabs-ai/ARGUS/issues/132) |
| E6 | miss | `email.body` blanked inside a declared `email` field is only a warning | `contextual` is top-level only | [#133](https://github.com/ArgusLabs-ai/ARGUS/issues/133) |
| E7 | miss | a ReAct final AI turn with `content: ""` and no tool calls is only a warning | inspector / message handling | [#134](https://github.com/ArgusLabs-ai/ARGUS/issues/134) |
| E8 | wrong blame | a router crash reading the node's own missing output blames the previous writer | `crash_origins` | [#135](https://github.com/ArgusLabs-ai/ARGUS/issues/135) |
| E9 | bystander | a linter reporting `errors: [...]` is blamed next to the node that fed it bad SQL | error-key rule | **fixed**: with E1, same change |

Suggested order: E7, E6, E8 (plain bugs) → the ambiguous tier described in
#130, then E3, E5 and E2 → E4b (needs a product call).

## 7. Current numbers (pivot_eval, after E4 / E1 / E9)

57 runs over 5 pipelines, 468 steps. 166 pass, 28 xfail (17 semantic gaps, 8
defects, 3 blame-precision issues). Of 14 healthy runs, 4 fail CI (E2 ×3, E3).
Of 26 rule-visible faults, 22 are caught at the origin; the 4 misses are E5 ×2,
E6 and E7. 0 of 17 semantic faults are caught. The reviewer judge was invoked
**0 times** across all 57 runs.

## 8. Live-LLM numbers (for the judge / classifier design)

Every case was repeated 3× at temperature 0, on synthetic data.

**Yes/no on an ambiguous flag** (the classifier's job; 15 labeled items: the
E1–E7 patterns plus their look-alikes): gpt-4o-mini got 14/15, identical on all 3
repeats, p50 1.3 s. Its one miss was a linter's `errors: [...]`. Any classifier
has to beat this on the same items.

**Parallel whole-trace judge** (the full fat trace in one call; a catch only
counts if that node is never flagged on the same pipeline's healthy runs):

| model | semantic gaps caught | healthy runs flagged | rule-visible faults caught |
|---|---|---|---|
| gpt-4o-mini | 9/17 | 6/14 (every one 3/3) | 4/26 |
| gpt-4.1 | 15/17 | 3/14 (every one 3/3) | 6/26 |

The false positives are consistent, not random, so majority voting does not
remove them. They are legitimate negative outcomes (a correct denial, a correct
empty result, a mismatch correctly sent to review). Neither model catches most
rule-visible faults, so the judge cannot replace the rules.

**Judge proposes, small model verifies** (gpt-4.1 findings, each re-checked by
gpt-4o-mini with only that node's I/O): the verifier kept 10/16 real catches
and removed only 1 of 3 false positives. A narrow verifier helps on narrow rule
flags (the yes/no row above), not on open-ended semantic claims.

**Ledger grounding rule** (prototype in `pivot_eval/grounding_probe.py`): every
ID and amount passed to a tool must already exist in earlier state or tool
output. 0/14 healthy runs flagged; catches the wrong-order refund, the
transposed claim ID and the refund larger than the order total.
