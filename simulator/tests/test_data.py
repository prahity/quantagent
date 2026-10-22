import pandas as pd

from sim.data.load_news import _conservative_available_at, sec_acceptance_to_utc


def test_sec_acceptance_quirk_is_corrected_for_est_and_edt():
    # Verified against EDGAR filing index pages ("Accepted" is Eastern local time).
    assert sec_acceptance_to_utc("2025-01-31T02:30:23.000Z") == pd.Timestamp("2025-01-30T21:30:23Z")  # 16:30:23 EST
    assert sec_acceptance_to_utc("2025-08-01T00:30:25.000Z") == pd.Timestamp("2025-07-31T20:30:25Z")  # 16:30:25 EDT
    assert sec_acceptance_to_utc("2025-01-31T16:01:27.000Z") == pd.Timestamp("2025-01-31T11:01:27Z")  # 06:01:27 EST


def test_after_hours_filing_becomes_available_next_morning():
    accepted = pd.Timestamp("2025-07-09T01:18:13Z")  # 21:18 EDT on July 8
    available = _conservative_available_at(accepted, "2025-07-09")
    assert available == pd.Timestamp("2025-07-09T10:00:00Z")  # 06:00 EDT
    same_day = _conservative_available_at(pd.Timestamp("2025-01-30T21:30:23Z"), "2025-01-30")
    assert same_day == pd.Timestamp("2025-01-30T21:30:23Z")


def test_fixtures_are_real_snapshot_slices(store):
    bars = store.bars("hourly", ["AAPL"], pd.Timestamp("2025-01-27", tz="UTC"), pd.Timestamp("2025-01-28", tz="UTC"))
    assert len(bars) == 7
    assert bars.iloc[0].ts == pd.Timestamp("2025-01-27T14:30:00Z")
    assert 220 < bars.iloc[0].open < 230  # AAPL traded in the mid-220s that morning
    news = store.news(["AAPL", "MSFT"], pd.Timestamp("2025-02-08", tz="UTC"))
    assert set(news.provider) == {"sec_edgar"} and news.availability_verified.all()
