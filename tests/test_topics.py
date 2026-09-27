import numpy as np

from groundswell.configs import SlowVariable, SlowVariablesFile
from groundswell.llm import parse_json
from groundswell.processing.classify import classify_text
from groundswell.processing.cluster import _UnionFind, similar_pairs
from groundswell.processing.embed import HashingEmbedding, item_text, parse_vec
from groundswell.processing.naming import NamingResult, heuristic_name
from groundswell.processing.slow_vars import match_slow_variables
from groundswell.processing.terms import count_matches, matches_any


def test_hash_embedding_is_normalized_and_semantic_enough():
    e = HashingEmbedding()
    m = e.embed(
        [
            "DeepSeek releases open model R3 for coding agents",
            "DeepSeek releases open model R3 coding agents benchmark",
            "Weather report for sailors calm seas",
            "",
        ]
    )
    assert m.shape == (4, 1024)
    assert np.allclose(np.linalg.norm(m[:3], axis=1), 1.0, atol=1e-5)
    sims = m @ m.T
    assert sims[0, 1] > e.assign_threshold > sims[0, 2]
    assert not np.isnan(m[3]).any()


def test_item_text_prefers_title():
    assert item_text("A long enough title that is descriptive on its own", "template body") == (
        "A long enough title that is descriptive on its own"
    )
    assert item_text("Short", "body text") == "Short\nbody text"


def test_parse_vec_variants():
    assert parse_vec("[1,2.5,-3]").tolist() == [1.0, 2.5, -3.0]
    assert parse_vec(np.array([1, 2])).dtype == np.float32


def test_similar_pairs_and_union_find():
    m = np.eye(4, dtype=np.float32)
    m[1] = m[0]
    m[3] = (m[2] + m[0]) / np.sqrt(2)
    pairs = similar_pairs(m, 0.9, block=2)
    assert pairs == [(0, 1, 1.0)] or [(p[0], p[1]) for p in pairs] == [(0, 1)]
    uf = _UnionFind()
    uf.union(3, 1)
    uf.union(1, 2)
    assert uf.find(3) == uf.find(2) == 1


def test_terms_word_boundary():
    assert matches_any("Best EV charging stations", ["ev"])
    assert not matches_any("every developer", ["ev"])
    assert matches_any("新能源电动车销量", ["电动车"])
    assert count_matches("AI agent for invoices", ["ai", "agent", "llm"]) == 2


def test_classify_text():
    assert classify_text("谢霆锋演唱会万人大合唱")[0] == "entertainment"
    assert classify_text("Show HN: open-source LLM inference server in Rust")[0] == "tech"
    assert classify_text("Is there an alternative to Notion? Looking for recommend")[0] == "demand"
    assert classify_text("国务院印发新能源汽车补贴政策")[0] == "policy"
    assert classify_text("xyz", ["consumer"])[0] == "consumer"
    assert classify_text("qwerty") == (None, 0.0)


def test_slow_variable_matching():
    sv = SlowVariablesFile(
        variables=[
            SlowVariable(id="ev", name="能源", description="", keywords=["ev", "电动车"]),
            SlowVariable(id="pet", name="宠物", description="", keywords=["宠物", "pet"]),
        ]
    )
    assert match_slow_variables("Cheap EV batteries", sv) == ["ev"]
    assert match_slow_variables("every developer", sv) == []
    assert match_slow_variables("宠物医院 电动车", sv) == ["ev", "pet"]


def test_heuristic_name_picks_keyword_rich_title():
    name, summary = heuristic_name(
        ["random", "DeepSeek R3 open model released", "R3"], ["deepseek", "r3", "open"], 3
    )
    assert name == "DeepSeek R3 open model released"
    assert "deepseek" in summary


def test_naming_schema_and_json_parsing():
    raw = '```json\n{"topics": [{"id": 1, "name": "AI 眼镜", "summary": "s", "category": "Tech"}]}\n```'
    result = NamingResult.model_validate(parse_json(raw))
    assert result.topics[0].category == "tech"
    assert parse_json('好的，结果如下：{"a": 1} 希望有帮助') == {"a": 1}
