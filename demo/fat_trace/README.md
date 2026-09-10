# Fat-trace demo — spike 1

The pivot's first milestone: a silent failure fails the build with **no engine wrap**.
No `patch_graph`, no patched `compile`, no rebound `invoke`. No API key, no network.

```bash
python demo/fat_trace/demo_graph.py
argus check          # exit 1 — silent_failure on summarize
```

`summarize` does its work, throws the result away and returns `{}`. LangGraph merges that
into a state that still has `docs`, so the run looks healthy and `answer` produces text.
Nothing crashes and the graph returns success.

`ArgusRecorder` rides LangGraph's callback stream, so it sees the **update** each node
returned rather than the merged state pile — which is the only place that `{}` is visible.
The existing inspector turns it into a critical `empty_output`, and blame lands on
`summarize`, not on `answer` downstream.

The whole user API is one line:

```python
app = ArgusRecorder().attach(app)
app.invoke({"query": "..."})
```
