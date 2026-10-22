from contracts.action import validate_action
from tests.conftest import act, hold, make_engine, order


def _errors(engine, payload):
    _, errors = validate_action(payload, engine.current_request, set(engine.symbols), engine.execution.max_orders_per_request)
    return {e.code for e in errors}


def test_hold_is_valid(store):
    engine = make_engine(store)
    assert _errors(engine, hold(engine.current_request)) == set()


def test_non_object_and_schema_errors(store):
    engine = make_engine(store)
    assert _errors(engine, "not json") == {"not_an_object"}
    assert "schema" in _errors(engine, {"schema_version": "1.0", "orders": "x"})
    bad = hold(engine.current_request)
    bad["extra"] = 1
    assert "schema" in _errors(engine, bad)


def test_identity_and_window_must_echo_request(store):
    engine = make_engine(store)
    r = engine.current_request
    p = hold(r)
    p["run_id"] = "other"
    assert "run_mismatch" in _errors(engine, p)
    p = hold(r)
    p["request_id"] = "decision_99999"
    assert "request_mismatch" in _errors(engine, p)
    p = hold(r)
    p["window_end"] = p["window_start"]
    assert "window_mismatch" in _errors(engine, p)
    p = hold(r)
    p["mode"] = "V2"
    assert "mode_mismatch" in _errors(engine, p)


def test_order_level_rules(store):
    engine = make_engine(store)
    r = engine.current_request
    assert "unknown_symbol" in _errors(engine, act(r, [order(r, "a", "ZZZZ", "BUY", 1)]))
    assert "duplicate_order_id" in _errors(engine, act(r, [order(r, "a", "AAPL", "BUY", 1), order(r, "a", "MSFT", "BUY", 1)]))
    assert "schema" in _errors(engine, act(r, [order(r, "a", "AAPL", "BUY", 0)]))
    assert "schema" in _errors(engine, act(r, [order(r, "a", "AAPL", "HOLD", 1)]))
    assert "bad_limit_price" in _errors(engine, act(r, [order(r, "a", "AAPL", "BUY", 1, order_type="LIMIT")]))
    assert "bad_limit_price" in _errors(engine, act(r, [order(r, "a", "AAPL", "BUY", 1, limit_price=100.0)]))
    assert "v1_timing" in _errors(engine, act(r, [order(r, "a", "AAPL", "BUY", 1, offset_s=10)]))
    too_many = [order(r, f"o{i}", "AAPL", "BUY", 1) for i in range(engine.execution.max_orders_per_request + 1)]
    assert "too_many_orders" in _errors(engine, act(r, too_many))


def test_v2_timing_window(store):
    engine = make_engine(store, mode="V2")
    r = engine.current_request
    assert _errors(engine, act(r, [order(r, "a", "AAPL", "BUY", 1, offset_s=0)])) == set()
    assert _errors(engine, act(r, [order(r, "a", "AAPL", "BUY", 1, offset_s=3599)])) == set()
    assert "v2_timing" in _errors(engine, act(r, [order(r, "a", "AAPL", "BUY", 1, offset_s=3600)]))
    assert "v2_timing" in _errors(engine, act(r, [order(r, "a", "AAPL", "BUY", 1, offset_s=-1)]))
