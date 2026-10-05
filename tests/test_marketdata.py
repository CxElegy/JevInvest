import pandas as pd

from src.marketdata import clean_bars

from conftest import FakeMarketData, make_bars


def _series(values):
    idx = pd.bdate_range("2026-01-05", periods=len(values))
    s = pd.Series(values, index=idx, dtype=float)
    return pd.DataFrame({"open": s, "high": s, "low": s, "close": s, "adj_close": s,
                         "volume": 1.0})


def test_feed_glitch_that_recovers_is_dropped_and_reported():
    # README の実例: 1306.T の 382.7 → 37.64 → 37.14 → 389.2
    df = _series([380, 381, 382.7, 37.64, 37.14, 389.2, 390, 391])
    out, warns = clean_bars("1306.T", df)

    assert len(out) == len(df) - 2
    assert out["close"].min() > 300
    assert len(warns) == 1 and "配信ミス" in warns[0] and "2 営業日" in warns[0]


def test_real_drop_that_does_not_recover_is_kept_but_warned():
    df = _series([100, 101, 100, 20, 21, 22, 23, 24, 25, 26])
    out, warns = clean_bars("XYZ", df)

    assert len(out) == len(df)                       # 勝手に直さない
    assert any("除外していません" in w for w in warns)


def test_normal_series_is_untouched():
    df = _series([100, 101, 102, 101, 103, 104])
    out, warns = clean_bars("OK", df)
    assert out.equals(df) and warns == []


def test_history_is_cached_and_glitch_warning_survives_the_cache(tmp_path):
    bars = make_bars(400.0, days=120, vol=0.002, seed=5)
    bars.iloc[60:62, bars.columns.get_indexer(["close", "adj_close"])] *= 0.1   # 配信ミス
    md = FakeMarketData(tmp_path / "m.sqlite", {"AAA": bars})

    first = md.get_history(["AAA"], years=1)["AAA"]
    assert len(first) == 118 and any("配信ミス" in e for e in md.fetch_errors)
    assert len(md.calls) == 1

    md.fetch_errors.clear()
    second = md.get_history(["AAA"], years=1)["AAA"]
    assert len(md.calls) == 1                        # 2 回目はネットワークに出ない
    assert len(second) == len(first)
    assert any("配信ミス" in e for e in md.fetch_errors)   # 警告はキャッシュ経由でも出る
    md.close()


def test_history_batches_all_symbols_into_one_download(market_data):
    market_data.get_history(["VT", "7203.T", "USDJPY=X"], years=1)
    assert len(market_data.calls) == 1 and len(market_data.calls[0]) == 3


def test_quotes_use_cache_and_report_failures_instead_of_zero(market_data):
    q = market_data.get_quotes(["VT", "NOPE"])
    assert "VT" in q and "NOPE" not in q
    assert any("NOPE" in e for e in market_data.fetch_errors)

    n = len(market_data.calls)
    market_data.get_quotes(["VT"])
    assert len(market_data.calls) == n               # TTL 内はキャッシュ


def test_empty_download_is_reported_not_silent(tmp_path):
    md = FakeMarketData(tmp_path / "m.sqlite", {})
    assert md.get_quotes(["VT"]) == {}
    assert any("空" in e for e in md.fetch_errors)
    md.close()


def test_fx_includes_base_currency(market_data):
    fx = market_data.get_fx(["JPY", "USD", "EUR"], "JPY")
    assert fx["JPY"] == 1.0 and "USD" in fx and "EUR" not in fx
