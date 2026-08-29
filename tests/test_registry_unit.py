"""Unit tests for argus.registry — signature matching strategies."""
import pytest

from argus.registry import (
    _match_contains_ci,
    _match_exact_ci,
    _match_prefix_ci,
    _match_repetition,
    get_registry,
    scan_value,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def registry():
    return get_registry()

def _make_sig(sid, pattern, strategy, category="placeholder_outputs", severity="warning"):
    sig = {
        "id": sid, "pattern": pattern, "match_strategy": strategy,
        "category": category, "severity": severity,
        "description": f"test sig {sid}",
    }
    if strategy == "regex":
        import re
        sig["_compiled"] = re.compile(pattern, re.IGNORECASE)
    return sig

@pytest.mark.unit
class TestExactCI:
    def test_exact_match(self):
        assert _match_exact_ci("N/A", "N/A") == (True, 1.0)

    def test_case_insensitive(self):
        assert _match_exact_ci("n/a", "N/A") == (True, 1.0)

    def test_whitespace_stripped(self):
        assert _match_exact_ci("N/A", "  N/A  ") == (True, 1.0)

    def test_no_match(self):
        assert _match_exact_ci("N/A", "Not applicable")[0] is False

@pytest.mark.unit
class TestContainsCI:
    def test_substring_match(self):
        matched, conf = _match_contains_ci("placeholder", "This is a placeholder value")
        assert matched is True
        assert 0 < conf <= 0.95

    def test_confidence_scales_with_ratio(self):
        _, short_conf = _match_contains_ci("placeholder text", "placeholder text")
        padded = "x" * 200 + "placeholder text" + "x" * 200
        _, long_conf = _match_contains_ci("placeholder text", padded)
        assert short_conf >= long_conf

@pytest.mark.unit
class TestRegex:
    def test_regex_match(self):
        sig = _make_sig("T-003", r"\d{3}-\d{4}", "regex")
        matches = scan_value("Call 555-1234 now", [sig])
        assert len(matches) == 1

    def test_critical_regex_high_confidence(self):
        sig = _make_sig("T-003", r"error", "regex", severity="critical")
        matches = scan_value("an error occurred", [sig])
        assert len(matches) == 1
        assert matches[0].confidence == 0.9

    def test_non_critical_regex_lower_confidence(self):
        sig = _make_sig("T-003", r"warning", "regex", severity="warning")
        matches = scan_value("a warning was issued", [sig])
        assert len(matches) == 1
        assert matches[0].confidence == 0.7

@pytest.mark.unit
class TestPrefixCI:
    def test_prefix_match(self):
        matched, conf = _match_prefix_ci("error:", "Error: something went wrong")
        assert matched is True

    def test_short_string_higher_confidence(self):
        _, short_conf = _match_prefix_ci("err", "Error")
        long_text = (
            "Error in the analysis of the complex data"
            " structure that was provided to the system"
        )
        _, long_conf = _match_prefix_ci("err", long_text)
        assert short_conf > long_conf

@pytest.mark.unit
class TestRepetition:
    def test_repetitive_text_detected(self):
        text = " ".join(["hello world"] * 20)
        matched, conf = _match_repetition(text)
        assert matched is True

    def test_normal_text_no_match(self):
        text = (
            "The quick brown fox jumps over the lazy dog."
            " Each word is unique in this longer sentence."
        )
        matched, _ = _match_repetition(text)
        assert matched is False

    def test_short_text_no_match(self):
        matched, _ = _match_repetition("hi")
        assert matched is False

@pytest.mark.unit
class TestCriticalShortCircuit:
    def test_stops_after_critical(self):
        sigs = [
            _make_sig("T-001", "FAIL", "exact_ci", severity="critical"),
            _make_sig("T-002", "fail", "contains_ci", severity="warning"),
        ]
        matches = scan_value("FAIL", sigs)
        assert len(matches) == 1
        assert matches[0].sig_id == "T-001"

@pytest.mark.unit
class TestScanValueFullRegistry:
    def test_registry_loads(self):
        reg = get_registry()
        assert len(reg) > 0
        categories = {s.get("category") for s in reg}
        assert "placeholder_outputs" in categories

    def test_normal_text_no_signals(self):
        matches = scan_value(
            "Tesla stock rose 5.3% today on strong Q3 earnings, "
            "beating analyst expectations by $0.12 per share.",
        )
        assert len(matches) == 0

    def test_placeholder_text_detected(self):
        matches = scan_value("PLACEHOLDER")
        assert any(m.sig_id.startswith("PH-") for m in matches)


@pytest.mark.unit
class TestCustomSignatures:
    def test_custom_signatures_loaded(self, tmp_path, monkeypatch):
        """Custom signatures from .argus/custom_signatures.json are appended."""
        import json

        from argus.registry import _load_registry

        monkeypatch.chdir(tmp_path)
        custom_dir = tmp_path / ".argus"
        custom_dir.mkdir(exist_ok=True)
        custom_sigs = {
            "signatures": [
                {
                    "id": "CUSTOM-001",
                    "pattern": "CUSTOM_PLACEHOLDER",
                    "match_strategy": "exact_ci",
                    "category": "placeholder_outputs",
                    "severity": "critical",
                    "description": "custom test sig",
                }
            ]
        }
        (custom_dir / "custom_signatures.json").write_text(json.dumps(custom_sigs))

        registry = _load_registry()
        custom_ids = [s["id"] for s in registry if s["id"] == "CUSTOM-001"]
        assert len(custom_ids) == 1

    def test_disabled_custom_signature_skipped(self, tmp_path, monkeypatch):
        import json

        from argus.registry import _load_registry

        monkeypatch.chdir(tmp_path)
        custom_dir = tmp_path / ".argus"
        custom_dir.mkdir(exist_ok=True)
        custom_sigs = {
            "signatures": [
                {
                    "id": "CUSTOM-002",
                    "pattern": "DISABLED_SIG",
                    "match_strategy": "exact_ci",
                    "category": "placeholder_outputs",
                    "severity": "warning",
                    "description": "disabled sig",
                    "metadata": {"disabled": True},
                }
            ]
        }
        (custom_dir / "custom_signatures.json").write_text(json.dumps(custom_sigs))

        registry = _load_registry()
        assert not any(s["id"] == "CUSTOM-002" for s in registry)

    def test_custom_regex_compiled(self, tmp_path, monkeypatch):
        import json

        from argus.registry import _load_registry

        monkeypatch.chdir(tmp_path)
        custom_dir = tmp_path / ".argus"
        custom_dir.mkdir(exist_ok=True)
        custom_sigs = {
            "signatures": [
                {
                    "id": "CUSTOM-003",
                    "pattern": r"ERROR_\d+",
                    "match_strategy": "regex",
                    "category": "placeholder_outputs",
                    "severity": "critical",
                    "description": "regex test",
                }
            ]
        }
        (custom_dir / "custom_signatures.json").write_text(json.dumps(custom_sigs))

        registry = _load_registry()
        custom = [s for s in registry if s["id"] == "CUSTOM-003"]
        assert len(custom) == 1
        assert "_compiled" in custom[0]
        assert custom[0]["_compiled"].match("ERROR_42")

    def test_malformed_custom_file_ignored(self, tmp_path, monkeypatch):
        from argus.registry import _load_registry

        monkeypatch.chdir(tmp_path)
        custom_dir = tmp_path / ".argus"
        custom_dir.mkdir(exist_ok=True)
        (custom_dir / "custom_signatures.json").write_text("not json{{{")

        registry = _load_registry()
        assert len(registry) > 0


@pytest.mark.unit
class TestNaturalUncertaintySignatures:
    def test_i_dont_know_hits(self, registry):
        matches = scan_value("I don't know.", registry)
        ids = {m.sig_id for m in matches}
        assert "SP-014" in ids

    def test_i_do_not_know_hits(self, registry):
        matches = scan_value("I do not know the answer.", registry)
        ids = {m.sig_id for m in matches}
        assert "SP-015" in ids

    def test_not_enough_information_hits(self, registry):
        matches = scan_value("I don't have enough information to answer that.", registry)
        ids = {m.sig_id for m in matches}
        assert "SP-016" in ids

    def test_know_alone_does_not_match(self, registry):
        matches = scan_value("Let me know if you need anything else.", registry)
        ids = {m.sig_id for m in matches}
        assert "SP-014" not in ids
        assert "SP-015" not in ids

    def test_na_not_newly_worsened(self, registry):
        """NL-007 still exact-matches 'Na'; new phrases must not contain-match it."""
        matches = scan_value("Na", registry)
        new_ids = {m.sig_id for m in matches if m.sig_id.startswith("SP-01")}
        assert not (new_ids & {"SP-014", "SP-015", "SP-016", "SP-017", "SP-018"})

    def test_n_slash_a_not_newly_worsened(self, registry):
        matches = scan_value("N/A", registry)
        new_ids = {m.sig_id for m in matches if m.sig_id in {"SP-014", "SP-015", "SP-016"}}
        assert not new_ids


@pytest.mark.unit
class TestEtcLazyTruncation:
    """SP-027 — enumeration abandoned with a trailing 'etc.'."""

    @pytest.mark.parametrize(
        "text",
        [
            "Pipeline stages: init, build, deploy, etc.",
            "Fields: id, name, email, etc",
            "Supported formats: json, yaml, toml, etc...",
            "Items: a, b, c, et cetera.",
            "Stages: init, build, ETC.",
        ],
    )
    def test_trailing_etc_hits(self, registry, text):
        ids = {m.sig_id for m in scan_value(text, registry)}
        assert "SP-027" in ids

    @pytest.mark.parametrize(
        "text",
        [
            "We use etc. for brevity in the list above.",
            "The /etc/passwd file was modified during the run.",
            "Config lives under /etc",
            "The fetcher retried twice before giving up.",
            "Sketch etcher tools are unrelated to this pipeline.",
        ],
    )
    def test_no_false_positive(self, registry, text):
        ids = {m.sig_id for m in scan_value(text, registry)}
        assert "SP-027" not in ids

    def test_severity_and_category(self, registry):
        sig = next(s for s in registry if s["id"] == "SP-027")
        assert sig["severity"] == "warning"
        assert sig["category"] == "suspicious_phrases"


@pytest.mark.unit
class TestDegradationPhraseSignatures:
    """SP-019..SP-026 — token-limit, self-reference, and lazy-truncation phrases."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("I have reached my token limit, so I'll stop here.", "SP-019"),
            ("The output was cut short due to length constraints.", "SP-020"),
            ("Note: the response was truncated by the provider.", "SP-021"),
            ("As mentioned above, the totals reconcile.", "SP-022"),
            ("As I said earlier, the retry failed.", "SP-023"),
            ("The config, as previously mentioned, is invalid.", "SP-024"),
            ("Affected files: a.py, b.py, c.py, and many more", "SP-025"),
            ("Pipeline stages: init, build, deploy, and so on.", "SP-026"),
        ],
    )
    def test_phrase_hits(self, registry, text, expected):
        ids = {m.sig_id for m in scan_value(text, registry)}
        assert expected in ids

    def test_all_are_warning_severity(self, registry):
        new = [s for s in registry if s["id"] in {f"SP-{n:03d}" for n in range(19, 27)}]
        assert len(new) == 8
        assert all(s["severity"] == "warning" for s in new)
        assert all(s["category"] == "suspicious_phrases" for s in new)

    @pytest.mark.parametrize(
        "text",
        [
            "Tesla stock rose 5.3% today on strong Q3 earnings, beating expectations.",
            "We iterate and so on the process converges only after several more runs.",
            "The dataset has many more rows than the sample shown in table 2.",
            "The token limit for this model is 128k; we stayed well under it.",
        ],
    )
    def test_no_false_positive_on_normal_prose(self, registry, text):
        ids = {m.sig_id for m in scan_value(text, registry)}
        assert not (ids & {f"SP-{n:03d}" for n in range(19, 27)})

    def test_lazy_truncation_only_matches_at_end(self, registry):
        """SP-025/026 are end-anchored — the phrase mid-sentence must not fire."""
        mid = "The list goes on and so on until the loop exits and then we return."
        ids = {m.sig_id for m in scan_value(mid, registry)}
        assert "SP-026" not in ids

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Affected files: a.py, b.py, c.py, and so on...", "SP-026"),
            ("Stages: init, build, deploy, and many more...", "SP-025"),
            ("Stages: init, build, deploy, and so on…", "SP-026"),
        ],
    )
    def test_lazy_truncation_matches_ellipsis(self, registry, text, expected):
        """SP-025/026 must catch the ellipsis-terminated form, not just a bare period."""
        ids = {m.sig_id for m in scan_value(text, registry)}
        assert expected in ids
