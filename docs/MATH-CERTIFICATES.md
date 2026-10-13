# Math certificates

ARGUS says its verdict aggregation is "auditable, not prompt-interpretable". This page backs that claim for
the one place where a wrong rule silently turns a failure into a pass: the behavior verdict fold,
`fold_behavior_verdicts` in `src/argus/models.py`.

## The model

The fold reads each clause judgment through three fields (`evaluated`, `verdict`, `na_reason`) and only through
`any` / `all` / emptiness. Every judgment therefore plays exactly one role:

| role | meaning | judgments |
|---|---|---|
| `F` | a violated clause | evaluated FALSE |
| `B` | a blocker: part of the contract could not be judged | not evaluated (any verdict), or evaluated NA with `insufficient_evidence` |
| `N` | an NA that hides nothing | evaluated NA with any other reason |
| `T` | a satisfied clause | evaluated TRUE |

With `cF, cB, cN, cT` the number of judgments in each role, the fold is

```
FALSE   if cF >= 1
NA      if cF + cB + cN + cT == 0, or cB >= 1, or cT == 0
TRUE    otherwise
```

## What was proved (all counts, not just small ones)

Each property below was posed to Z3 as a quantifier-free linear-integer query (`smt.solve`, logic `QF_LIA`, via
Jacobian) over non-negative integers `cF, cB, cN, cT`, as the **negation** of the property. `UNSAT` means no
counterexample exists for any clause counts at all. Three further queries are `SAT`, so the proofs are not vacuous.

With `V(cF,cB,cN,cT) = (ite (>= cF 1) 2 (ite (or (= (+ cF cB cN cT) 0) (>= cB 1)) 0 (ite (= cT 0) 0 1)))`
(2 = FALSE, 0 = NA, 1 = TRUE; `define-fun` is not admitted, so the query text is macro-expanded):

| # | property | query asserts | result |
|---|---|---|---|
| P1 | TRUE is only ever the result when nothing is violated or unjudged and a clause was satisfied | `V = 1` and `not (cF = 0 and cB = 0 and cT >= 1)` | UNSAT |
| P2 | a violated clause always wins | `cF >= 1` and `V != 2` | UNSAT |
| P3 | an unjudged clause can never make a behavior pass | `V(cF, cB+1, cN, cT) = 1` | UNSAT |
| P4 | an unjudged clause never improves the verdict (FALSE < NA < TRUE) | `rank(V with cB+1) > rank(V)` | UNSAT |
| P5 | adding a violated clause always gives FALSE | `V(cF+1, cB, cN, cT) != 2` | UNSAT |
| P6 | no judgments is never a verdict | `cF+cB+cN+cT = 0` and `V != 0` | UNSAT |
| P7 | only NAs that hide nothing is never a verdict | `cF = cB = cT = 0`, `cN >= 1`, `V != 0` | UNSAT |
| S1-S3 | non-vacuity: TRUE, NA and FALSE are each reachable | `V = 1`, `V = 0`, `V = 2` | SAT, SAT, SAT |

P3 is the property reviews kept finding broken in earlier versions of the fold (unjudged clauses silently dropped,
so one satisfied clause plus an unjudged one passed).

To re-check, run each query through any Jacobian server (`math.run`, operation `smt.solve`).

## The real code equals the model

SMT proves facts about the model; `tests/test_fold_laws.py` checks the *real* function against it. Because the
fold depends only on which judgment kinds are present, never on how many or in what order, checking all 2^14
subsets of the 14 distinct kinds covers every possible list of judgments of any length. The same file checks
order independence and that repeating a judgment changes nothing, and it fails against three deliberately
broken folds (unjudged clauses ignored; an unjudged clause outranking a violated one; all-NA read as TRUE).

## What this does and does not establish

- It establishes how judgments are *combined*, for every number of clauses.
- It does not establish that a judgment was assigned the right role (that is the judge and its citation / quote /
  completeness rules, covered by their own tests), nor anything about LLM behavior.
- A result of `UNKNOWN`, a timeout, or a missing counterexample from a bounded search would not be a proof; here
  every proof query returned a definite `UNSAT` from the solver.
