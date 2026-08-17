// The proposed pilot site (PROJECT.md §5): semantic_checker.check_semantic_coherence.
//
// This mirrors the schema the live system prompt in src/argus/semantic_checker.py
// already asks for, so the spike compares like with like. It is NOT a proposed
// final prompt — the point is to exercise the transport with a realistic
// schema, not to tune wording.

class DisambiguationVerdict {
  sig_id string
  is_failure bool
  confidence float
  reason string
}

class SemanticCheck {
  // The live wire format uses "pass", which is a Python keyword. @alias keeps
  // the JSON key identical to what the current prompt asks the model for
  // while giving the generated Python class a legal attribute name.
  passed bool @alias("pass")
  reason string
  confidence float
  evidence_considered string[]
  overridden_signals string[]
  disambiguation_verdicts DisambiguationVerdict[]
}

function CheckSemanticCoherence(
  node_name: string,
  input_state: string,
  output_state: string,
  prior_signals: string,
  ambiguous_matches: string
) -> SemanticCheck {
  client ArgusHostedProxy
  prompt #"
    {{ _.role("system") }}
    You verify whether an AI pipeline node produced semantically correct
    output given its input.

    - "pass": true if the output is a reasonable response to the input
    - "pass": false ONLY if the output is completely unrelated, contradictory,
      or nonsensical given the input
    - Do not judge quality or completeness, only semantic relevance
    - If you cannot determine relevance (insufficient context), pass it
    - When in doubt, PASS. False positives are worse than false negatives.

    {{ ctx.output_format }}

    {{ _.role("user") }}
    Node: "{{ node_name }}"
    Input: {{ input_state }}
    Output: {{ output_state }}

    Prior Signals: {{ prior_signals }}
    Ambiguous Heuristic Matches: {{ ambiguous_matches }}
  "#
}
