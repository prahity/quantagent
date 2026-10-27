"""Explore cannot see the test window, accounts stay isolated, and rules repeat."""
from __future__ import annotations

import pytest

import meterless.experiment as experiment
from agent.scripted import AccumulatingBuyAndHold, make_order
from meterless.query import QueryRequest, SqliteSource
from meterless.strategy import Signal, Selection, StrategySpec, _python_features, feature_sql, load_features
from sim.service import SimulatorService
from sim.store import SnapshotStore


FIXTURES = experiment.SIM / "tests" / "fixtures"


def _bars() -> list[dict]:
    rows = []
    for offset, close in enumerate(range(100, 130)):
        day = f"2024-01-{offset + 1:02d}"
        rows.append({
            "symbol": "AAPL", "interval": "daily", "ts": f"{day}T00:00:00Z",
            "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1000,
        })
    rows.append({
        "symbol": "AAPL", "interval": "daily", "ts": "2024-02-01T00:00:00Z",
        "open": 9999, "high": 9999, "low": 9999, "close": 9999, "volume": 1000,
    })
    return rows


def _filings() -> list[dict]:
    return [
        {"symbol": "AAPL", "form": "10-Q", "title": "released", "published_at": "2024-01-15T12:00:00Z", "available_at": "2024-01-15T12:00:00Z"},
        {"symbol": "AAPL", "form": "8-K", "title": "dated ahead", "published_at": "2024-02-15T12:00:00Z", "available_at": "2024-01-20T12:00:00Z"},
        {"symbol": "AAPL", "form": "10-K", "title": "not released", "published_at": "2024-03-01T12:00:00Z", "available_at": "2024-03-01T12:00:00Z"},
    ]


def test_feature_view_stops_at_as_of(tmp_path):
    source = SqliteSource.build(tmp_path / "lake.sqlite", _bars(), _filings())
    as_of = "2024-02-01T14:30:00Z"
    sql = feature_sql(["AAPL"], as_of)
    assert "feed_prices" not in sql and "_released" not in sql
    rows = load_features(source, as_of, ["AAPL"])
    assert load_features.last_error == ""
    assert rows and rows[0]["symbol"] == "AAPL"
    assert rows[0]["close"] != 9999
    assert rows[0]["close"] == 129
    assert rows[0]["ret_5"] == pytest.approx(129 / 124 - 1)
    assert rows[0]["last_filing"] == "2024-01-15"
    assert rows[0]["next_filing"] == ""
    assert rows[0]["days_since_filing"] == 17
    released = source.execute(QueryRequest(
        run_id="t", request_id="t", query_id="filings", goal="cutoff",
        sql="SELECT title FROM filings",
        as_of=as_of,
    ), row_limit=20)
    titles = [row["title"] for row in released.rows]
    assert titles == ["released"]
    direct = source.execute(QueryRequest(
        run_id="t", request_id="t", query_id="t", goal="cutoff",
        sql="SELECT close FROM bars WHERE interval = 'daily' AND symbol = 'AAPL' ORDER BY ts",
        as_of=as_of,
    ), row_limit=100)
    assert 9999 not in [row["close"] for row in direct.rows]


def test_short_history_stays_null(tmp_path):
    bars = []
    for offset in range(8):
        close = 100 + offset
        day = f"2024-01-{offset + 2:02d}"
        bars.append({
            "symbol": "AAPL", "interval": "daily", "ts": f"{day}T00:00:00Z",
            "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 100,
        })
    source = SqliteSource.build(tmp_path / "short.sqlite", bars, [])
    source.feature_symbols = ["AAPL"]
    as_of = "2024-01-10T14:30:00Z"
    rows = load_features(source, as_of, ["AAPL"])
    assert load_features.last_error == ""
    row = rows[0]
    assert row["ret_5"] is not None
    assert row["ret_21"] is None and row["ret_63"] is None
    assert row["vol_21"] is None and row["ma_gap_21"] is None and row["ma_gap_63"] is None and row["dd_63"] is None
    via_sql = source.execute(QueryRequest(
        run_id="t", request_id="t", query_id="feat", goal="features",
        sql="SELECT close, ret_5, ret_21, vol_21, ma_gap_63, dd_63 FROM features",
        as_of=as_of,
    ))
    assert via_sql.error == ""
    assert via_sql.rows[0]["close"] == 107
    assert via_sql.rows[0]["ret_21"] is None and via_sql.rows[0]["vol_21"] is None
    assert via_sql.rows[0]["dd_63"] is None
    python_rows = _python_features(source, as_of, ["AAPL"])
    assert python_rows[0]["vol_21"] is None and python_rows[0]["dd_63"] is None and python_rows[0]["ret_5"] is not None


def test_rolling_waits_until_april():
    rounds = experiment.rolling_rounds()
    assert rounds[0]["explore_start"] == "2024-01-01"
    assert rounds[0]["explore_end"] == "2024-03-31"
    assert rounds[0]["test_start"] == "2024-04-01"
    assert rounds[-1]["test_start"] == "2024-12-01"
    assert all(item["test_start"] >= "2024-04-01" for item in rounds)
    assert experiment.parse_window("2024-01..2024-03") == ("2024-01-01", "2024-03-31")


def test_test_notebook_has_no_test_window():
    explore = "Highest explore profit was 12.50 on 2024-01-02 to 2024-01-31. sample_days=21. The sample is uncertain."
    assert experiment.notes_for_test(explore) == explore
    assert experiment.leaks_test_window(explore, "2024-02-01") is False
    assert experiment.leaks_test_window(explore + " 2024-02-01", "2024-02-01") is True
    rows = [{"arm": "explore", "spec_id": "root-s01", "window_end": "2024-01-31", "net_profit": 12.5}]
    experiment.assert_rows_before(rows, "2024-02-01")
    with pytest.raises(ValueError):
        experiment.assert_rows_before([{"window_end": "2024-02-01"}], "2024-02-01")
    with pytest.raises(ValueError):
        experiment.assert_allowed_years("2025-01-01", "2025-01-31", "")
    experiment.assert_allowed_years("2025-01-01", "2025-01-31", "validation")


def _write_run(root, run_id: str, equities: list[float]) -> None:
    import json

    directory = root / run_id
    directory.mkdir(parents=True)
    days = [f"2024-01-{index + 2:02d}T14:30:00Z" for index in range(len(equities))]
    (directory / "equity.jsonl").write_text(
        "\n".join(json.dumps({"ts": day, "cash": value * 0.2, "equity": value}) for day, value in zip(days, equities)) + "\n")
    (directory / "cash_events.jsonl").write_text("")


class _Store:
    def bars(self, interval, symbols, start, end):
        import pandas as pd

        rows = []
        for offset in range(10):
            ts = pd.Timestamp("2024-01-02", tz="UTC") + pd.Timedelta(days=offset)
            rows.append({"symbol": "AAPL", "ts": ts, "close": 100 + offset})
            rows.append({"symbol": "SPY", "ts": ts, "close": 100 - offset * 0.5})
        frame = pd.DataFrame(rows)
        return frame[(frame.ts >= start) & (frame.ts < end) & frame.symbol.isin(symbols)]


def _spec(name: str, kind: str, lookback: int) -> dict:
    spec = StrategySpec(name=name, universe=["AAPL", "SPY"], signal=Signal(kind=kind, lookback=lookback)).model_dump()
    spec["spec_id"] = name
    return spec


def test_notebook_lists_top_and_worst_rules_with_parameters_and_confidence(tmp_path):
    from meterless import explore_notebook

    curves = {
        "bench": [10000 + 10 * day for day in range(10)],
        "a": [10000 + 30 * day for day in range(10)],
        "b": [10000 + 20 * day + (5 if day % 2 else 0) for day in range(10)],
        "c": [10000 + 12 * day for day in range(10)],
        "d": [10000 - 5 * day for day in range(10)],
        "e": [10000 - 10 * day for day in range(10)],
        "f": [10000 - 20 * day for day in range(10)],
    }
    for run_id, values in curves.items():
        _write_run(tmp_path, run_id, values)
    kinds = {"a": ("momentum", 21), "b": ("momentum", 5), "c": ("relative_strength", 21),
             "d": ("volatility", 21), "e": ("mean_reversion", 5), "f": ("mean_reversion", 21)}
    specs = {name: _spec(name, *kind) for name, kind in kinds.items()}
    profits = {"a": 270, "b": 185, "c": 108, "d": -45, "e": -90, "f": -180}
    rows = [{"arm": "explore", "spec_id": name, "run_id": name, "window_end": "2024-01-31", "net_profit": profit,
             "twr": profit / 100, "max_drawdown": 0.0 if profit > 0 else 1.8, "turnover": 0.5 if name != "f" else 3.0}
            for name, profit in profits.items()]
    bench = {"run_id": "bench", "twr": 0.9, "max_drawdown": 0.2}
    earlier = explore_notebook.oos_record(
        "2024-01", "2024-01-01", "2024-01-31",
        {"name": "a", "spec_id": "old-a", "spec": specs["a"], "rank": 1},
        {"twr": 1.0, "max_drawdown": 0.3, "net_profit": 90}, {"twr": 0.4},
    )
    unfinished = {**earlier, "test_month": "2024-02", "test_start": "2024-02-01", "test_end": "2024-02-29"}
    nb = explore_notebook.build_notebook(
        notebook_id="nb-1", parent_id=None, round_id="2024-02",
        explore_start="2024-01-01", explore_end="2024-01-31", test_start="2024-02-01",
        sessions=10, rows=rows, bench=bench, specs=specs,
        market=explore_notebook.market_conditions(_Store(), ["AAPL", "SPY"], "2024-01-01", "2024-01-31"),
        track=[earlier, unfinished], runs_dir=tmp_path, cash_annual_rate=0.05,
    )
    assert [item["name"] for item in nb["top"]] == ["a", "b", "c"]
    assert [item["name"] for item in nb["worst"]] == ["f", "e", "d"]
    assert nb["top"][0]["params"]["signal"] == "momentum" and nb["top"][0]["params"]["lookback"] == 21
    assert nb["top"][0]["excess_pp"] == pytest.approx(1.8)
    assert nb["worst"][0]["why"][0].startswith("excess vs buy-and-hold -2.70 pp")
    assert any("turnover 3.00" in reason for reason in nb["worst"][0]["why"])
    assert nb["track_record"] == [earlier]
    assert nb["top"][0]["oos"] == [earlier]
    assert nb["market"]["symbols"]["AAPL"]["return_pct"] == pytest.approx(9.0)
    assert nb["market"]["trend"] == "up"
    rec = nb["recommendation"]
    assert rec["action"] == "apply_rule" and rec["rule"] == "a"
    assert rec["confidence"]["level"] in ("low", "medium", "high")
    text = explore_notebook.render_notebook(nb)
    assert "small sample" not in text.lower()
    assert "What to do: " in text and "Confidence: " in text
    assert "10 explore sessions, 6 rules tried" in text
    assert "momentum lookback 21, top 2" in text
    assert experiment.leaks_test_window(text, "2024-02-01") is False

    for run_id in ("a", "b"):
        (tmp_path / run_id / "fills.jsonl").write_text('{"filled_at": "2024-01-03", "side": "BUY", "symbol": "AAPL", "quantity": 3}\n')
    merged = explore_notebook.build_notebook(
        notebook_id="nb-2", parent_id=None, round_id="2024-02",
        explore_start="2024-01-01", explore_end="2024-01-31", test_start="2024-02-01",
        sessions=10, rows=rows, bench=bench, specs=specs, market=nb["market"],
        track=[], runs_dir=tmp_path, cash_annual_rate=0.05,
    )
    assert [item["name"] for item in merged["top"]] == ["a", "c", "d"]
    assert merged["top"][0]["same_trades_as"] == ["b"]
    assert merged["n_specs"] == 6 and merged["n_distinct"] == 5


def test_propose_repairs_a_missing_brace_and_a_dangling_key():
    import json

    rule = '"universe":["AAPL"],"signal":{"kind":"momentum","lookback":21},"selection":{"mode":"top_n","top_n":1},"target_weight":0.3,"cash_floor":0.1,"rebalance_every":5,"min_weight_change":0.05,"drawdown_cut":0.12'
    missing = '{"strategies":[{"name":"a",' + rule + '},"name":"b",' + rule + '}]}'
    dangling = '{"strategies":[{"name":"a",' + rule + '},"description":"x"},{"name":"b",' + rule + '}]}'
    for broken in (missing, dangling):
        with pytest.raises(json.JSONDecodeError):
            json.loads(broken)
        names = [item["name"] for item in json.loads(experiment._repair_strategies(broken))["strategies"]]
        assert names == ["a", "b"]

    replies = iter([missing, "not json"])

    class Runtime:
        def complete(self, _content, max_tokens=None):
            return next(replies)

    specs = experiment.propose_specs(Runtime(), "", ["AAPL", "MSFT"], 2, None)
    assert [spec.name for spec in specs] == ["a", "b"]
    assert all(spec.origin == "model" for spec in specs)
    assert experiment.propose_specs.last_calls == 1

    replies = iter([missing, "x", "x"])
    specs = experiment.propose_specs(Runtime(), "", ["AAPL", "MSFT"], 4, None)
    assert [spec.origin for spec in specs] == ["model", "model", "fallback", "fallback"]

    class Broken:
        def complete(self, _content, max_tokens=None):
            return "not json"

    specs = experiment.propose_specs(Broken(), "", ["AAPL", "MSFT"], 3, None)
    assert [spec.origin for spec in specs] == ["fallback"] * 3
    assert experiment.propose_specs.last_calls == experiment._PROPOSE_ATTEMPTS


def test_feature_cache_is_shared_saved_per_version_and_returns_identical_rows(tmp_path):
    source = SqliteSource.build(tmp_path / "lake.sqlite", _bars(), _filings())
    times = ["2024-01-25T14:30:00Z", "2024-02-01T14:30:00+00:00"]
    direct = [load_features(source, as_of, ["AAPL"]) for as_of in times]
    path = experiment.feature_cache_path("sqlite", "snap-a", ["AAPL"]).relative_to(experiment.REPO)
    path = tmp_path / path

    first = experiment.FeatureCache(source, ["AAPL"], path)
    first.prefetch(times)
    assert first.fetched == 2
    assert [first(as_of) for as_of in times] == direct
    assert first.hits == 2

    class NoLake:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("a cached session went back to the lake")

    second = experiment.FeatureCache(NoLake(), ["AAPL"], path)
    assert second.loaded == 2
    second.prefetch(times)
    assert second.fetched == 0
    assert [second("2024-01-25T14:30:00Z"), second("2024-02-01T14:30:00Z")] == direct

    other = tmp_path / experiment.feature_cache_path("sqlite", "snap-b", ["AAPL"]).relative_to(experiment.REPO)
    assert other != path
    assert experiment.FeatureCache(source, ["AAPL"], other).loaded == 0


@pytest.fixture
def lake_up():
    experiment._LAKE_DOWN.clear()
    yield
    experiment._LAKE_DOWN.clear()


def test_a_failed_feature_view_stops_the_run_and_is_never_cached(tmp_path, monkeypatch, lake_up):
    from meterless.query import LakeError

    def boom(*_args, **_kwargs):
        raise RuntimeError("lake down")

    monkeypatch.setattr(experiment, "load_features", boom)
    path = tmp_path / "cache.jsonl"
    cache = experiment.FeatureCache(None, ["AAPL"], path)
    with pytest.raises(LakeError, match="lake down"):
        cache("2024-01-25T14:30:00Z")
    assert "2024-01-25T14:30:00Z" not in cache
    assert not path.exists()
    with pytest.raises(LakeError, match="earlier lake call"):
        experiment.LockedSource(SqliteSource.build(tmp_path / "lake.sqlite", _bars(), _filings())).execute(
            QueryRequest(run_id="t", request_id="t", query_id="t", goal="g", sql="SELECT 1", as_of="2024-01-25T14:30:00Z"))


def test_load_features_raises_when_the_view_and_its_fallback_both_fail():
    from meterless.query import LakeError, QueryResult

    class Broken:
        name = "zetaris"
        data_version = "x"

        def execute(self, request, row_limit=None):
            return QueryResult(query_id=request.query_id, goal="", sql=request.sql, as_of=request.as_of,
                               backend="zetaris", data_version="x", columns=[], rows=[], truncated=False,
                               error="engine stopped", elapsed_ms=0.0)

    with pytest.raises(LakeError, match="engine stopped"):
        load_features(Broken(), "2024-01-25T14:30:00Z", ["AAPL"])


def test_a_lake_error_in_an_arm_is_raised_not_scored(monkeypatch, lake_up):
    from meterless.query import LakeError

    def lake_fails(*_args, **_kwargs):
        raise LakeError("Zetaris lake unreachable after 4 tries")

    monkeypatch.setattr(experiment, "simulate", lake_fails)
    with pytest.raises(LakeError):
        experiment._run_one(None, {}, None, phase="test", round_id="2024-10", arm="best_rule", repeat=1,
                            start="", end="", spec_id="s", parent_id=None)
    assert experiment._LAKE_DOWN.is_set()

    experiment._LAKE_DOWN.clear()

    def bug(*_args, **_kwargs):
        raise ValueError("not a lake problem")

    monkeypatch.setattr(experiment, "simulate", bug)
    row = experiment._run_one(None, {}, None, phase="test", round_id="2024-10", arm="nomem", repeat=1,
                              start="", end="", spec_id="s", parent_id=None)
    assert "not a lake problem" in row["error"]
    with pytest.raises(experiment.RoundFailed, match="nomem#1"):
        experiment.check_round("2024-10", [{"arm": "cash", "repeat": 1, "net_profit": 50.0}, row])
    experiment.check_round("2024-10", [{"arm": "cash", "repeat": 1, "net_profit": 50.0}])


def test_memory_agent_does_not_hide_a_lake_error_in_its_rule_view():
    from meterless.query import LakeError
    from meterless.trader import NemotronTrader

    def view(_request, _feed):
        raise LakeError("feature view at 2024-10-01 failed")

    trader = NemotronTrader(object(), memory=5, notes="", retries=0, rule_view=view)
    with pytest.raises(LakeError):
        trader._rules({"request_id": "r1"}, None)


def test_resume_restores_finished_rounds_and_refuses_changed_settings(tmp_path):
    rounds = experiment.rolling_rounds(2024)[:3]
    config = {"symbols": ["AAPL"], "rounds": [spec["round"] for spec in rounds], "explore_rounds": 3,
              "specs": 12, "repeats": 2, "model": "m", "thinking": "off", "data_version": "snap-a"}
    experiment.check_run_config(tmp_path, config, resume=False)
    nb = {"notebook_id": "nb-1", "text": "notebook one"}
    rows = [{"round": rounds[0]["round"], "phase": "test", "arm": "memory", "net_profit": 1.5}]
    records = [{"spec_id": "s1", "excess_pp": 0.4}]
    proposals = [{"round": rounds[0]["round"], "explore_round": 1, "nemotron": 10, "fallback": 2, "propose_calls": 2}]
    experiment.save_round(tmp_path, rounds[0], nb, rows, records, proposals, 12.0)
    experiment.save_round(tmp_path, rounds[2], nb, rows, records, proposals, 12.0)

    finished = experiment.load_finished_rounds(tmp_path, rounds)
    assert [item["round"] for item in finished] == [rounds[0]["round"]]
    assert finished[0]["notebook_text"] == "notebook one"
    assert finished[0]["rows"] == rows and finished[0]["records"] == records
    assert finished[0]["proposals"] == proposals

    with pytest.raises(SystemExit):
        experiment.check_run_config(tmp_path, config, resume=False)
    with pytest.raises(SystemExit):
        experiment.check_run_config(tmp_path, {**config, "repeats": 3}, resume=True)
    experiment.check_run_config(tmp_path, config, resume=True)

    experiment.write_proposals(tmp_path / "proposals.csv", proposals)
    text = (tmp_path / "proposals.csv").read_text(encoding="utf-8").splitlines()
    assert text == ["round,explore_round,nemotron,fallback,propose_calls", f"{rounds[0]['round']},1,10,2,2"]


def test_2025_validation_rounds_roll_explore_forward_and_stop_before_each_test_month():
    rounds = experiment.month_rounds("2024-01", "2025-01..2025-06")
    assert [spec["round"] for spec in rounds] == ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06"]
    assert all(spec["explore_start"] == "2024-01-01" for spec in rounds)
    assert rounds[0]["explore_end"] == "2024-12-31" and rounds[0]["test_start"] == "2025-01-01"
    assert rounds[2]["explore_end"] == "2025-02-28" and rounds[2]["test_end"] == "2025-03-31"
    for spec in rounds:
        assert spec["explore_end"] < spec["test_start"]
        experiment.assert_allowed_years(spec["explore_start"], spec["test_end"], "validation")
        with pytest.raises(ValueError):
            experiment.assert_allowed_years(spec["explore_start"], spec["test_end"], "")
    with pytest.raises(ValueError):
        experiment.assert_allowed_years("2025-01-01", "2026-01-31", "validation")
    assert experiment.rolling_rounds(2024)[0]["round"] == "2024-04"


def _advisory_nb(track: list[dict]) -> dict:
    from meterless.explore_notebook import rule_params

    top = {
        "rank": 1, "name": "mom_5_t2", "spec_id": "s1", "params": rule_params(_spec("mom_5_t2", "momentum", 5)),
        "twr": 18.7, "excess_pp": -1.2, "max_drawdown": 9.0, "turnover": 40.0, "avg_invested_pct": 55.0,
        "daily_excess_t": 1.1, "oos": [], "same_trades_as": ["mom_5_t2_copy"], "confidence": {"level": "low", "basis": "t 1.1"},
    }
    worst = {**top, "rank": 12, "name": "bad_rule", "why": ["lagged"]}
    return {
        "notebook_id": "nb-2025-02-r3-a1b2", "parent_id": "nb-2025-02-r2", "explore_start": "2024-01-01",
        "explore_end": "2025-01-31", "sessions": 273, "n_specs": 36, "n_distinct": 20,
        "market": {"trend": "up", "equal_weight_return_pct": 20.0, "volatility": "normal", "mean_ann_vol_pct": 18.0,
                   "symbols": {"SPY": {"return_pct": 22.0, "ann_vol_pct": 12.0, "max_drawdown_pct": 8.0}}},
        "buy_and_hold": {"twr": 25.0, "max_drawdown": 10.0}, "cash_twr": 5.0,
        "top": [top], "worst": [worst], "track_record": track,
        "recommendation": {"rule": "mom_5_t2", "confidence": {"level": "low", "basis": "t 1.1"}, "instruction": "Apply mom_5_t2 as written."},
    }


def test_advisory_notebook_is_advice_with_rules_record_confidence_and_regime():
    from meterless import explore_notebook

    track = [{"test_month": "2025-01", "name": "mom_5_t2", "explore_rank": 1, "twr": 1.0, "bench_twr": 2.0, "excess_pp": -1.0}]
    nb = _advisory_nb(track)
    text = explore_notebook.advisory_text(nb)
    assert "Market regime: trend up" in text
    assert "1. mom_5_t2 [s1]" in text and "Confidence low" in text
    assert "2025-01 mom_5_t2 (explore rank 1)" in text
    assert "bad_rule" not in text and "Apply mom_5_t2" not in text and "What to do" not in text
    assert not experiment.leaks_test_window(text, "2025-02-01")
    assert experiment.leaks_test_window(text, "2025-01-15")
    assert explore_notebook.advice_rule_names(nb) == ["mom_5_t2", "mom_5_t2_copy"]


def test_dry_run_counts_the_advisory_arm_only_when_asked():
    rounds = experiment.month_rounds("2024-01", "2025-01..2025-02")
    base = experiment.planned_llm(rounds, 3, 2, [252, 273], [21, 19])
    advisory = experiment.planned_llm(rounds, 3, 2, [252, 273], [21, 19],
                                      arms=experiment._DEFAULT_ARMS + ("memory_advisory",))
    assert base["advisory_decision_calls"] == 0
    assert advisory["advisory_decision_calls"] == round(40 * 2 * experiment._ADVISORY_CALLS_PER_DECISION)
    assert advisory["llm_calls"] == base["llm_calls"] + advisory["advisory_decision_calls"]
    assert advisory["estimated_test_model_seconds"] > base["estimated_test_model_seconds"]


def test_resume_treats_an_older_config_as_the_default_arms(tmp_path):
    import json

    old = {"symbols": ["AAPL"], "rounds": ["2024-04"], "explore_rounds": 3, "specs": 12, "repeats": 2,
           "model": "m", "thinking": "off", "data_version": "snap-a"}
    (tmp_path / "run_config.json").write_text(json.dumps(old), encoding="utf-8")
    experiment.check_run_config(tmp_path, {**old, "arms": list(experiment._DEFAULT_ARMS)}, resume=True)
    with pytest.raises(SystemExit, match="arms"):
        experiment.check_run_config(tmp_path, {**old, "arms": list(experiment._DEFAULT_ARMS) + ["memory_advisory"]}, resume=True)


def test_progress_log_has_round_phase_and_elapsed(tmp_path):
    import json

    progress = experiment.Progress(tmp_path / "progress.jsonl")
    with progress.phase("2024-04", "propose", explore_round=1) as info:
        info["nemotron"] = 12
    lines = [json.loads(line) for line in (tmp_path / "progress.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(line["round"], line["phase"], line["status"]) for line in lines] == [
        ("2024-04", "propose", "start"), ("2024-04", "propose", "done"),
    ]
    assert lines[1]["nemotron"] == 12 and "seconds" in lines[1] and "elapsed_s" in lines[1]


def test_confidence_needs_out_of_sample_wins_for_high():
    from meterless.explore_notebook import confidence

    assert confidence(3.0, 61, 12, [])["level"] == "medium"
    assert confidence(1.0, 61, 12, [])["level"] == "low"
    wins = [{"excess_pp": 1.0}] * 3
    assert confidence(3.0, 61, 12, wins)["level"] == "high"
    assert confidence(1.6, 61, 12, wins[:2])["level"] == "medium"
    assert confidence(1.6, 61, 12, [{"excess_pp": 1.0}, {"excess_pp": -1.0}])["level"] == "low"


def test_accounts_are_isolated():
    import pandas as pd

    store = SnapshotStore(
        {name: pd.read_parquet(FIXTURES / f"bars_{name}.parquet") for name in ("hourly", "daily")},
        pd.read_parquet(FIXTURES / "news.parquet"),
        data_version="fixture",
    )
    left = SimulatorService(store, persist=False)
    right = SimulatorService(store, persist=False)
    base = {
        "interval": "hourly", "mode": "V1", "start": "2025-01-28", "end": "2025-01-28",
        "symbols": ["AAPL"], "initial_cash": 10000, "daily_deposit": 200, "cash_annual_rate": 0.05,
        "execution": {"max_position_pct_equity": 0},
    }
    request_left = left.reset({**base, "run_id": "iso-left", "label": "left"})
    request_right = right.reset({**base, "run_id": "iso-right", "label": "right"})
    assert request_left["run_id"] != request_right["run_id"]
    cash_right = right.state("iso-right")["cash"]
    order = make_order(request_left, "buy1", "AAPL", "BUY", 1)
    payload = {
        "schema_version": "1.0", "run_id": request_left["run_id"], "request_id": request_left["request_id"],
        "mode": "V1", "window_start": request_left["window_start"], "window_end": request_left["window_end"],
        "orders": [order], "reason": "isolation",
    }
    left.submit_action("iso-left", payload)
    assert left.state("iso-left")["cash"] < cash_right
    assert right.state("iso-right")["cash"] == cash_right
    assert right.state("iso-right")["positions"] == []
    assert left.engines["iso-left"] is not right.engines["iso-right"]


def _features(_as_of: str) -> list[dict]:
    return [
        {"symbol": "AAPL", "ret_5": 0.04, "ret_21": 0.08, "ret_63": 0.1, "vol_21": 0.01, "ma_gap_21": 0.02, "ma_gap_63": 0.03, "dd_63": 0.01, "rs_spy": 0.02, "days_since_filing": 4},
        {"symbol": "MSFT", "ret_5": -0.02, "ret_21": 0.01, "ret_63": 0.02, "vol_21": 0.03, "ma_gap_21": -0.01, "ma_gap_63": 0.0, "dd_63": 0.04, "rs_spy": -0.01, "days_since_filing": 30},
    ]


def test_rule_runner_is_deterministic():
    import pandas as pd

    store = SnapshotStore(
        {name: pd.read_parquet(FIXTURES / f"bars_{name}.parquet") for name in ("hourly", "daily")},
        pd.read_parquet(FIXTURES / "news.parquet"),
        data_version="fixture",
    )
    spec = StrategySpec(
        name="mom-5",
        universe=["AAPL", "MSFT"],
        signal=Signal(kind="momentum", lookback=5),
        selection=Selection(mode="top_n", top_n=1),
        target_weight=0.30,
        cash_floor=0.10,
        rebalance_every=1,
        min_weight_change=0.01,
        drawdown_cut=0.50,
    ).model_dump()
    orders = []
    profits = []
    for run_id in ("rule-a", "rule-b"):
        service = SimulatorService(store, persist=False)
        cfg = experiment.run_config("2025-01-28", "2025-02-04", ["AAPL", "MSFT"], "rule", run_id)
        cfg["interval"] = "daily"
        agent = experiment.RuleRunner(spec, _features)
        seen = []

        class Recording:
            def decide(self, request, feed):
                payload = agent.decide(request, feed)
                seen.append([(item["symbol"], item["side"], item["quantity"]) for item in payload["orders"]])
                return payload

        first = service.reset(cfg)
        feed = experiment.LocalFeedReader(service, first["run_id"])
        report = experiment.run_agent(Recording(), lambda _cfg: first, service.submit_action, feed, cfg, verbose=False)
        orders.append(seen)
        profits.append(report["net_profit"])
    assert orders[0] == orders[1]
    assert orders[0]
    assert profits[0] == profits[1]


class _Marks:
    def __init__(self, marks):
        self._marks = marks

    def marks(self):
        return self._marks


def _request(step: int, cash: float, deposit: float) -> dict:
    return {
        "run_id": "bha",
        "request_id": f"r{step}",
        "mode": "V1",
        "step": step,
        "window_start": "2024-01-02T14:30:00Z",
        "window_end": "2024-01-03T14:30:00Z",
        "account": {"cash": cash, "deposit_today": deposit, "equity": cash},
        "data": {"symbols": ["AAPL", "MSFT", "SPY"]},
        "limits": {"max_position_pct_equity": 0, "slippage_bps": 1},
    }


def test_accumulating_buy_and_hold_waits_for_a_whole_share():
    agent = AccumulatingBuyAndHold()
    pricey = {"AAPL": {"close": 4000}, "MSFT": {"close": 4000}, "SPY": {"close": 4000}}
    first = agent.decide(_request(0, 300, 0), _Marks(pricey))
    assert first["orders"] == []
    purse = agent.purse
    second = agent.decide(_request(1, 500, 200), _Marks(pricey))
    assert second["orders"] == []
    assert agent.purse == pytest.approx(purse + 200)
    later = agent.decide(_request(2, 5000, 4500), _Marks({"AAPL": {"close": 100}, "MSFT": {"close": 4000}, "SPY": {"close": 4000}}))
    assert any(order["side"] == "BUY" and order["symbol"] == "AAPL" and order["quantity"] >= 1 for order in later["orders"])
    assert all(order["order_id"].startswith("bha_") for order in later["orders"])
