"""Tests for the benchmark measurement helpers: ablation switch, run comparison, MCP cold-start summary."""

import json

import pytest

from benchmark import compare as cmp
from benchmark import mcp_coldstart, run_inference


class TestMcNemar:
    def test_no_discordant_pairs(self):
        assert cmp.mcnemar_p(0, 0) == 1.0

    def test_balanced_is_not_significant(self):
        assert cmp.mcnemar_p(5, 5) == 1.0

    def test_one_sided_is_significant(self):
        assert cmp.mcnemar_p(10, 0) == pytest.approx(2 / 1024)

    def test_symmetric(self):
        assert cmp.mcnemar_p(7, 2) == cmp.mcnemar_p(2, 7)


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


class TestCompare:
    def test_won_lost_on_common_questions(self, tmp_path):
        base = _write(tmp_path / "evaluated_full.jsonl", [
            {"question_id": 1, "ex": True, "ves": True, "latency_ms": 100},
            {"question_id": 2, "ex": False, "ves": True, "latency_ms": 300},
            {"question_id": 3, "ex": True, "ves": True, "latency_ms": 200},
            {"question_id": 4, "ex": True, "ves": True, "latency_ms": 200},  # not in other run
        ])
        other = _write(tmp_path / "evaluated_no_okf.jsonl", [
            {"question_id": 1, "ex": False, "ves": False, "latency_ms": 50, "without": ["okf"]},
            {"question_id": 2, "ex": True, "ves": True, "latency_ms": 70, "without": ["okf"]},
            {"question_id": 3, "ex": False, "ves": True, "latency_ms": 60, "without": ["okf"]},
        ])
        report = cmp.compare([base, other])

        assert report["questions"] == 3
        full, no_okf = report["runs"]
        assert full["label"] == "full" and full["ex"] == pytest.approx(66.67)
        assert no_okf["label"] == "no_okf (without okf)"
        assert no_okf["won"] == ["2"] and no_okf["lost"] == ["1", "3"]
        assert no_okf["ves"] == pytest.approx(66.67)
        assert no_okf["median_latency_ms"] == 60
        assert no_okf["mcnemar_p"] == 1.0

    def test_unevaluated_file_is_rejected(self, tmp_path):
        raw = _write(tmp_path / "full.jsonl", [{"question_id": 1, "predicted_sql": "SELECT 1"}])
        with pytest.raises(ValueError, match="benchmark.evaluate"):
            cmp.compare([raw, raw])


class TestAblate:
    def test_switches_off_okf_and_glossary(self):
        import main
        from MetadataManager import OKFStore

        original_search, original_glossary = OKFStore.search_concepts, main._get_business_context
        with run_inference.ablate(["okf", "glossary"]):
            assert OKFStore.search_concepts("q", [], None, None) == []
            assert main._get_business_context("q") == []
        assert OKFStore.search_concepts is original_search
        assert main._get_business_context is original_glossary

    def test_nothing_patched_by_default(self):
        from MetadataManager import OKFStore

        original = OKFStore.search_concepts
        with run_inference.ablate():
            assert OKFStore.search_concepts is original

    def test_unknown_value_rejected(self):
        with pytest.raises(ValueError, match="unknown --without"):
            with run_inference.ablate(["schema"]):
                pass

    def test_records_ablation_and_drops_evidence(self, tmp_path, monkeypatch):
        import main

        seen = {}

        def fake_generate(**kwargs):
            seen.update(kwargs)
            return {"type": "sql", "content": "SELECT 1"}

        monkeypatch.setattr(main, "generateQuery", fake_generate)
        monkeypatch.setattr(run_inference, "_load_bird_questions", lambda *a, **k: [{
            "question_id": 7, "db_id": "shop", "question": "how many?", "evidence": "count rows",
            "gold_sql": "SELECT 1", "difficulty": "simple",
        }])
        out = tmp_path / "no_evidence.jsonl"
        run_inference.run_inference("bird", tmp_path, "open_ai", out, without=["evidence"])

        record = json.loads(out.read_text().splitlines()[0])
        assert record["without"] == ["evidence"]
        assert [m["content"] for m in seen["conversation"]] == ["how many?"]

    def test_resume_refuses_mixed_ablations(self, tmp_path, monkeypatch):
        monkeypatch.setattr(run_inference, "_load_bird_questions", lambda *a, **k: [])
        out = _write(tmp_path / "run.jsonl", [{"question_id": 1, "result_type": "sql", "without": ["okf"]}])
        with pytest.raises(ValueError, match="separate --output_file"):
            run_inference.run_inference("bird", tmp_path, "open_ai", out, without=[])


class TestColdStartSummary:
    def test_median_min_max(self):
        runs = [{"initialize_ms": 900.0, "first_search_ms": 12000.0, "warm_search_ms": 80.0},
                {"initialize_ms": 1100.0, "first_search_ms": 10000.0, "warm_search_ms": None},
                {"initialize_ms": 1000.0, "first_search_ms": 11000.0, "warm_search_ms": 60.0}]
        summary = mcp_coldstart.summarise(runs)
        assert summary["initialize_ms"] == {"median": 1000.0, "min": 900.0, "max": 1100.0}
        assert summary["first_search_ms"]["median"] == 11000.0
        assert summary["warm_search_ms"] == {"median": 70.0, "min": 60.0, "max": 80.0}
        assert "total_ms" not in summary
