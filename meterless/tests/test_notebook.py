from __future__ import annotations

import json

import pytest

from meterless.notebook import apply, update


NOTES = "\n".join([
    "r1: Enter after a 5-day high.",
    "evidence: 2024-01 MSFT +6.34%. Unproven.",
    "",
    "r2: Size each entry at 20% of equity.",
    "evidence: 2024-01 both names reached the 25% cap.",
])


def test_unnamed_rules_stay_word_for_word():
    raw = json.dumps({"amend": [{"id": "r1", "evidence": "2024-02 MSFT +2.94%. Still unproven."}]})
    updated = apply(NOTES, raw)
    assert "r2: Size each entry at 20% of equity.\nevidence: 2024-01 both names reached the 25% cap." in updated
    assert updated.index("r1:") < updated.index("r2:")
    assert "2024-01 MSFT +6.34%. Unproven." in updated
    assert "2024-02 MSFT +2.94%. Still unproven." in updated


def test_add_appends_and_drop_removes_only_the_named_rule():
    raw = json.dumps({
        "add": [{"text": "Deploy cash when a name is trending up.", "evidence": "2024-02 idle cash while SPY rose. Unproven."}],
        "drop": [{"id": "r2", "because": "2024-02 the 20% size left cash idle and buy-and-hold was ahead."}],
    })
    updated = apply(NOTES, raw)
    assert "r2:" not in updated
    assert "r1: Enter after a 5-day high." in updated
    assert "r3: Deploy cash when a name is trending up." in updated


def test_a_drop_of_a_missing_rule_does_not_block_an_add():
    updated = apply("", json.dumps({
        "drop": [{"id": "r1", "because": "the notebook was empty"}],
        "add": [{"text": "Buy symbols whose latest close is higher than the close 21 sessions earlier.", "evidence": "2024-02 cash sat idle. Unproven."}],
    }))
    assert updated.startswith("r1: Buy symbols whose latest close is higher")


def test_an_amend_of_a_missing_rule_is_saved_as_a_new_rule():
    updated = apply("", json.dumps({"amend": [{"id": "r1", "text": "Buy when the close is higher than the prior close.", "evidence": "2024-01 the agent held cash. Unproven."}]}))
    assert updated.startswith("r1: Buy when the close is higher than the prior close.")
    assert "2024-01 the agent held cash. Unproven." in updated


def test_a_rewritten_notebook_is_rejected():
    with pytest.raises(ValueError):
        apply(NOTES, "r1: A completely new notebook.\nr2: Another new rule.")
    with pytest.raises(ValueError):
        apply(NOTES, json.dumps({"notebook": "r1: replacement"}))
    kept = apply(NOTES, json.dumps({"drop": [{"id": "r9", "because": "gone"}]}))
    assert "r1: Enter after a 5-day high." in kept
    assert "r2: Size each entry at 20% of equity." in kept


def test_update_sends_the_current_notebook_and_applies_the_reply():
    class Runtime:
        def complete(self, content: str) -> str:
            self.content = content
            return json.dumps({"add": [{"text": "Hold no more than five days of cash in an up month.", "evidence": "2024-01 days_fully_in_cash was high. Unproven."}]})

    runtime = Runtime()
    text = update(runtime, "", {"period": "2024-01-01 to 2024-01-31"})
    assert "Update the strategy notebook" in runtime.content or "You update the strategy notebook" in runtime.content
    assert "Rewrite" not in runtime.content
    assert "(empty, this is the first period)" in runtime.content
    assert text == "\n".join([
        "r1: Hold no more than five days of cash in an up month.",
        "evidence: 2024-01 days_fully_in_cash was high. Unproven.",
    ])
