# Auto-generated regression contracts

**Status:** not built · **Verdict:** build next · **Size:** medium (~1–2 weeks)

## The idea

Tag a successful run as a "golden trace". ARGUS parses it and learns a structural contract for
every node — this node always returns three keys, this field is always a non-empty list, this node
is always reached. On the next pre-deployment pass, a prompt change that makes the agent skip a
node or return a different shape fails the build.

The pitch: nobody wants to hand-write assertions for every node transition in a graph. Learned
assertions beat written ones.

## What already exists

- **Every input and output is already recorded.** `NodeEvent.input_state` and
  `NodeEvent.output_dict` (`models.py`) hold the full state per step. A contract can be *derived*
  from a stored run — no new instrumentation needed.
- **Type-level contract checking already runs.** `inspector.py` has `inspect_transition`, which
  reads successor type annotations and reports `FieldMismatch` when output types do not match the
  next node's expected input. `InspectionResult.type_mismatches` carries the result.
- **Manual assertions exist.** `validators={"node": lambda o: (bool, msg)}` covers the
  hand-written case, which is exactly what this feature aims to replace.
- **Run persistence and reload is solved.** `storage.save_run` / `load_run`, with schema
  versioning (`schema_version`) and a deserialization fallback already in place.

## What's genuinely missing

1. **A golden-trace tag.** No concept of marking a run as the reference. Needs a field on
   `RunRecord` (or a sidecar file in `.argus/`), a CLI verb, and a UI affordance.
2. **A contract inference pass.** Walk a golden run and emit, per node: key set, per-field type,
   emptiness, list lengths where stable, and which nodes were reached in what order. The
   interesting design question is which observations generalize from a single run and which are
   coincidence — inferring from N golden runs is far more defensible than from one.
3. **A contract checker.** Compare a new run against the stored contract, emit violations.
4. **A non-zero exit path for CI.** ARGUS has no "fail the build" mode today.

## Why it's parked

Only because state patching came first — this is the strongest of the parked ideas.

The thing to get right is (2). Inferring a contract from exactly one run will produce brittle
assertions that fail on harmless variation, and a testing tool that cries wolf gets turned off.
Recommend requiring several golden runs before a field is promoted to a hard assertion, and
distinguishing "always observed" from "observed once".

Note the prerequisite ordering: the golden-trace tag from (1) is also what
[smoke-test-runner.md](smoke-test-runner.md) needs for its expected-output baseline. Build it once,
in a shape that serves both.
