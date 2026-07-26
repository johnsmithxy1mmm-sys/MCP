"""H5: self-learning matcher — calibrate confidence from resolution ground truth."""

from __future__ import annotations

import pytest

from core.matchlearn import MatchLearnStore


def test_record_and_resolve_agreement(tmp_path):
    store = MatchLearnStore(db_url=f"sqlite:///{tmp_path / 'm.db'}")
    store.record("a", "b", 0.6)   # a and b matched with confidence 0.6
    store.record("c", "d", 0.6)
    # a & b resolved the SAME (true match); c & d diverged (false match).
    n = store.resolve({"a": 1, "b": 1, "c": 1, "d": 0})
    assert n == 2
    samples = dict((round(c, 2), agreed) for c, agreed in store.samples())
    assert samples == {0.6: 1} or len(store.samples()) == 2  # both at conf 0.6
    agreed = [agreed for _, agreed in store.samples()]
    assert sorted(agreed) == [0, 1]


def test_calibrate_identity_until_min_samples(monkeypatch, tmp_path):
    monkeypatch.setenv("MATCHLEARN_MIN_SAMPLES", "10")
    store = MatchLearnStore(db_url=f"sqlite:///{tmp_path / 'm.db'}")
    store.record("a", "b", 0.6)
    store.resolve({"a": 1, "b": 1})
    out = store.calibrate(0.6)
    assert out["learned"] is False
    assert out["calibrated_confidence"] == 0.6  # identity on thin evidence


def test_calibrate_learns_from_history(monkeypatch, tmp_path):
    monkeypatch.setenv("MATCHLEARN_MIN_SAMPLES", "6")
    monkeypatch.setenv("MATCHLEARN_BINS", "10")
    store = MatchLearnStore(db_url=f"sqlite:///{tmp_path / 'm.db'}")

    # High-similarity pairs (0.85) almost always agreed; low ones (0.45) rarely did.
    def seed(conf, n, agree):
        with store._connect() as conn:
            for i in range(n):
                conn.execute("INSERT INTO match_obs (pair_id, a, b, confidence, resolved, agreed) "
                             "VALUES (?,?,?,?,1,?)",
                             (f"{conf}-{i}", f"a{conf}{i}", f"b{conf}{i}", conf,
                              1 if i < agree else 0))
    seed(0.85, 10, 9)   # 90% agreed
    seed(0.45, 10, 2)   # 20% agreed

    hi = store.calibrate(0.85)
    lo = store.calibrate(0.45)
    assert hi["learned"] is True
    assert hi["calibrated_confidence"] > lo["calibrated_confidence"]
    assert hi["calibrated_confidence"] == pytest.approx(0.9, abs=0.1)
    # A monotone calibrator: the high-similarity bucket maps higher.
    assert lo["calibrated_confidence"] < 0.5


@pytest.mark.asyncio
async def test_compare_reports_calibrated_confidence(client):
    r = (await client.call_tool(
        "compare_across_venues", {"event": "bitcoin above 100k end of 2026"}
    )).data
    assert r["matched"] is True
    assert "calibrated_confidence" in r
    assert "calibrated_confidence" in r["calibrated_confidence"]  # nested block


@pytest.mark.asyncio
async def test_resolve_outcomes_teaches_matcher(client):
    from predmarket_mcp import deps
    from core.matchlearn import get_matchlearn

    deps.record_match("mkt-a", "mkt-b", 0.7)
    deps.resolve_outcomes({"mkt-a": 1, "mkt-b": 1})  # same outcome -> true match
    assert get_matchlearn().samples() == [(0.7, 1)]
