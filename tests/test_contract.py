"""Fixed JSON envelope: V1/V2 validation on the agent side."""
import unittest

from agent import contract
from tests._util import stub_request


class ContractTest(unittest.TestCase):
    def test_valid_v1_envelope(self):
        req = stub_request("V1")
        orders = [contract.make_order(req, "o1", "AAPL", "BUY", 1)]
        env = contract.build_envelope(req, orders, "ok")
        self.assertEqual(contract.validate_envelope(env, req, {"AAPL", "MSFT"}, 20), [])

    def test_hold_is_valid(self):
        req = stub_request("V1")
        self.assertEqual(contract.validate_envelope(contract.hold(req), req, {"AAPL"}, 20), [])

    def test_v1_timing_must_equal_boundary(self):
        req = stub_request("V1")
        order = contract.make_order(req, "o1", "AAPL", "BUY", 1)
        order["execute_at"] = "2025-01-28T14:30:30Z"  # not the boundary
        env = contract.build_envelope(req, [order])
        self.assertTrue(any("V1 execute_at" in e for e in contract.validate_envelope(env, req, {"AAPL"}, 20)))

    def test_v2_timing_inside_window(self):
        req = stub_request("V2")
        inside = contract.make_order(req, "o1", "AAPL", "BUY", 1, offset_seconds=30)
        self.assertEqual(contract.validate_envelope(contract.build_envelope(req, [inside]), req, {"AAPL"}, 20), [])
        outside = contract.make_order(req, "o2", "AAPL", "BUY", 1)
        outside["execute_at"] = "2025-01-28T15:30:00Z"  # == window_end, excluded
        errs = contract.validate_envelope(contract.build_envelope(req, [outside]), req, {"AAPL"}, 20)
        self.assertTrue(any("V2 execute_at" in e for e in errs))

    def test_unknown_symbol_and_bad_quantity(self):
        req = stub_request("V1")
        bad_sym = contract.make_order(req, "o1", "ZZZZ", "BUY", 1)
        self.assertTrue(any("unknown symbol" in e for e in
                            contract.validate_envelope(contract.build_envelope(req, [bad_sym]), req, {"AAPL"}, 20)))
        bad_qty = contract.make_order(req, "o2", "AAPL", "BUY", 1)
        bad_qty["quantity"] = 0
        self.assertTrue(any("quantity" in e for e in
                            contract.validate_envelope(contract.build_envelope(req, [bad_qty]), req, {"AAPL"}, 20)))

    def test_mismatched_ids_and_too_many_orders(self):
        req = stub_request("V1")
        env = contract.build_envelope(req, [])
        env["run_id"] = "other"
        self.assertTrue(any("run_id" in e for e in contract.validate_envelope(env, req, {"AAPL"}, 20)))
        many = [contract.make_order(req, f"o{i}", "AAPL", "BUY", 1) for i in range(3)]
        self.assertTrue(any("exceeds max" in e for e in
                            contract.validate_envelope(contract.build_envelope(req, many), req, {"AAPL"}, 2)))

    def test_market_limit_price_rules(self):
        req = stub_request("V1")
        o = contract.make_order(req, "o1", "AAPL", "BUY", 1)
        o["limit_price"] = 10.0  # MARKET must be null
        self.assertTrue(any("MARKET" in e for e in
                            contract.validate_envelope(contract.build_envelope(req, [o]), req, {"AAPL"}, 20)))

    def test_non_object_payload(self):
        req = stub_request("V1")
        self.assertTrue(contract.validate_envelope(["not", "an", "object"], req, {"AAPL"}, 20))


if __name__ == "__main__":
    unittest.main()
