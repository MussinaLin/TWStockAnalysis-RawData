"""Unit tests: 單檔區間 OHLCV 預取（無網路）。

核心風險：www.twse.com.tw 被限流時回 HTTP 200 + stat「很抱歉，沒有符合條件的資料!」，
與「該月真的沒資料」是同一個字串，無法從回應區分（見
memory/twse-rate-limit-ambiguous-response.md）。

唯一可用的外部訊號是 MoneyDJ zcl：它整段只打一發、不經 TWSE，若它證明該月有交易
而交易所月表回空，就判定為限流／取得失敗，該月不寫並警告 —— 而不是靜默當成沒交易。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
import requests

from tw_stock_rawdata import run
from tw_stock_rawdata.sources import DataUnavailableError

START = dt.date(2025, 6, 10)
END = dt.date(2025, 8, 5)


def test_month_starts_covers_partial_edges() -> None:
    assert run._month_starts(START, END) == [
        dt.date(2025, 6, 1), dt.date(2025, 7, 1), dt.date(2025, 8, 1),
    ]


def test_month_starts_single_month() -> None:
    assert run._month_starts(dt.date(2025, 7, 3), dt.date(2025, 7, 20)) == [
        dt.date(2025, 7, 1)
    ]


def test_month_starts_crosses_year_boundary() -> None:
    assert run._month_starts(dt.date(2025, 12, 20), dt.date(2026, 1, 5)) == [
        dt.date(2025, 12, 1), dt.date(2026, 1, 1),
    ]


def _twse_month_df(day: int) -> pd.DataFrame:
    return pd.DataFrame(
        [["114/07/%02d" % day, "24,000,000", "30,000,000", "1250.00",
          "1260.00", "1245.00", "1255.00", "-5.00", "50,000", ""]],
        columns=["日期", "成交股數", "成交金額", "開盤價", "最高價", "最低價",
                 "收盤價", "漲跌價差", "成交筆數", "註記"],
    )


def test_twse_symbol_fetches_month_tables(monkeypatch) -> None:
    calls: list[dt.date] = []

    def fake(session, stock_no, date):  # noqa: ANN001 - 測試替身
        calls.append(date)
        return _twse_month_df(15)

    monkeypatch.setattr(run, "fetch_twse_stock_day", fake)

    out = run._prefetch_symbol_ohlcv(
        session=None, symbol="2330", market_type="twse",
        start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31),
        traded_dates=set(),
    )

    assert calls == [dt.date(2025, 7, 1)]
    assert out.market_type == "twse"
    assert out.failed_months == []
    assert out.by_date[dt.date(2025, 7, 15)].close == 1255.0


def test_tpex_symbol_never_calls_twse(monkeypatch) -> None:
    """上櫃股不可打 TWSE 月表 —— 那支 API 沒有上櫃資料，只是白耗限流配額。"""
    twse_calls: list[str] = []
    monkeypatch.setattr(
        run, "fetch_twse_stock_day",
        lambda *a, **k: twse_calls.append("x") or pd.DataFrame(),
    )
    monkeypatch.setattr(
        run, "fetch_tpex_stock_day",
        lambda session, stock_no, date: pd.DataFrame(
            [["114/07/16", "3,709", "1,184,624", "310.00", "326.50",
              "307.50", "322.50", "18.50", "5,095"]],
            columns=["日 期", "成交張數", "成交仟元", "開盤", "最高", "最低",
                     "收盤", "漲跌", "筆數"],
        ),
    )

    out = run._prefetch_symbol_ohlcv(
        session=None, symbol="6488", market_type="tpex",
        start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31),
        traded_dates=set(),
    )

    assert twse_calls == []
    assert out.by_date[dt.date(2025, 7, 16)].volume == 3_709_000
    assert out.by_date[dt.date(2025, 7, 16)].change == 18.5


def test_empty_month_without_moneydj_evidence_is_treated_as_no_trading(
    monkeypatch,
) -> None:
    """MoneyDJ 也沒有該月資料 → 該檔那個月本來就沒交易，不算失敗。"""
    monkeypatch.setattr(
        run, "fetch_twse_stock_day",
        lambda *a, **k: (_ for _ in ()).throw(DataUnavailableError("無資料")),
    )

    out = run._prefetch_symbol_ohlcv(
        session=None, symbol="2330", market_type="twse",
        start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31),
        traded_dates=set(),
    )

    assert out.by_date == {}
    assert out.failed_months == []


def test_empty_month_with_moneydj_evidence_is_flagged_as_failure(
    monkeypatch,
) -> None:
    """MoneyDJ 證明該月有交易，兩個市場的月表都回空 → 判定限流／取得失敗。"""
    monkeypatch.setattr(
        run, "fetch_twse_stock_day",
        lambda *a, **k: (_ for _ in ()).throw(DataUnavailableError("無資料")),
    )
    monkeypatch.setattr(
        run, "fetch_tpex_stock_day",
        lambda *a, **k: (_ for _ in ()).throw(DataUnavailableError("無資料")),
    )

    out = run._prefetch_symbol_ohlcv(
        session=None, symbol="2330", market_type="twse",
        start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31),
        traded_dates={dt.date(2025, 7, 15)},
    )

    assert out.by_date == {}
    assert out.failed_months == [dt.date(2025, 7, 1)]


# ---------------------------------------------------------------------------
# 跨市場補救（上櫃轉上市／上市轉上櫃）
# ---------------------------------------------------------------------------


def _tpex_month_df(day: int) -> pd.DataFrame:
    return pd.DataFrame(
        [["114/06/%02d" % day, "3,709", "1,184,624", "310.00", "326.50",
          "307.50", "322.50", "18.50", "5,095"]],
        columns=["日 期", "成交張數", "成交仟元", "開盤", "最高", "最低",
                 "收盤", "漲跌", "筆數"],
    )


def test_transferred_symbol_recovers_early_months_from_other_market(
    monkeypatch,
) -> None:
    """上櫃轉上市：轉換前的月份只在 TPEX 有，不可判成限流。

    `stocks.market_type` 只記得轉換後的市場（twse），轉換前的月份打 TWSE 必定
    回空，而 MoneyDJ 不分市場照樣有列 —— 沒有跨市場補救就會被判成限流，叫操作者
    重跑一個永遠不會成功的區間。
    """
    twse_months: list[dt.date] = []
    tpex_months: list[dt.date] = []

    def fake_twse(session, stock_no, date):  # noqa: ANN001 - 測試替身
        twse_months.append(date)
        if date == dt.date(2025, 7, 1):
            return _twse_month_df(15)
        raise DataUnavailableError("很抱歉，沒有符合條件的資料!")

    def fake_tpex(session, stock_no, date):  # noqa: ANN001 - 測試替身
        tpex_months.append(date)
        if date == dt.date(2025, 6, 1):
            return _tpex_month_df(16)
        raise DataUnavailableError("無資料")

    monkeypatch.setattr(run, "fetch_twse_stock_day", fake_twse)
    monkeypatch.setattr(run, "fetch_tpex_stock_day", fake_tpex)

    out = run._prefetch_symbol_ohlcv(
        session=None, symbol="6488", market_type="twse",
        start=dt.date(2025, 6, 1), end=dt.date(2025, 7, 31),
        traded_dates={dt.date(2025, 6, 16), dt.date(2025, 7, 15)},
    )

    # 轉換前後的資料都拿到了，且沒有誤報限流
    assert out.by_date[dt.date(2025, 6, 16)].close == 322.5
    assert out.by_date[dt.date(2025, 7, 15)].close == 1255.0
    assert out.failed_months == []
    # 定調的市場別不因單月補救而翻轉：它要寫回 holdings 供處置註記使用
    assert out.market_type == "twse"
    # 補救只發生在「回空且 MoneyDJ 有交易」的那一個月，正常月份零額外請求
    assert twse_months == [dt.date(2025, 6, 1), dt.date(2025, 7, 1)]
    assert tpex_months == [dt.date(2025, 6, 1)]


def test_no_cross_market_probe_when_month_has_no_moneydj_evidence(
    monkeypatch,
) -> None:
    """沒有 MoneyDJ 證據的空月份不觸發跨市場補救 —— 那是上市前的正常空月。"""
    tpex_calls: list[str] = []
    monkeypatch.setattr(
        run, "fetch_twse_stock_day",
        lambda *a, **k: (_ for _ in ()).throw(DataUnavailableError("無資料")),
    )
    monkeypatch.setattr(
        run, "fetch_tpex_stock_day",
        lambda *a, **k: tpex_calls.append("x") or pd.DataFrame(),
    )

    run._prefetch_symbol_ohlcv(
        session=None, symbol="2330", market_type="twse",
        start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31),
        traded_dates=set(),
    )

    assert tpex_calls == []


# ---------------------------------------------------------------------------
# 訊息紀律：預期中的空月份不吵，傳輸失敗一定講
# ---------------------------------------------------------------------------


def test_expected_empty_month_prints_nothing(monkeypatch, capsys) -> None:
    """回補上市前的月份會有幾十個空月，不可每個都印一行失敗訊息 ——
    那會把同一個輸出流裡的 ⚠ 限流警告訓練成雜訊。"""
    monkeypatch.setattr(
        run, "fetch_twse_stock_day",
        lambda *a, **k: (_ for _ in ()).throw(DataUnavailableError("無資料")),
    )

    run._prefetch_symbol_ohlcv(
        session=None, symbol="2330", market_type="twse",
        start=dt.date(2025, 1, 1), end=dt.date(2025, 7, 31),
        traded_dates=set(),
    )

    assert capsys.readouterr().out == ""


def test_failure_message_carries_the_fetch_reason(monkeypatch, capsys) -> None:
    """判定失敗時才印，且要帶上原始錯誤訊息（判限流與判其他狀況的唯一線索）。"""
    monkeypatch.setattr(
        run, "fetch_twse_stock_day",
        lambda *a, **k: (_ for _ in ()).throw(
            DataUnavailableError("很抱歉，沒有符合條件的資料!")
        ),
    )
    monkeypatch.setattr(
        run, "fetch_tpex_stock_day",
        lambda *a, **k: (_ for _ in ()).throw(DataUnavailableError("無資料")),
    )

    run._prefetch_symbol_ohlcv(
        session=None, symbol="2330", market_type="twse",
        start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31),
        traded_dates={dt.date(2025, 7, 15)},
    )

    out = capsys.readouterr().out
    assert "⚠" in out
    assert "很抱歉，沒有符合條件的資料!" in out


def test_transport_failure_is_always_printed(monkeypatch, capsys) -> None:
    """RequestException 是真的出事了（連線／HTTP 層），即使該月本來就沒交易也要印。"""
    monkeypatch.setattr(
        run, "fetch_twse_stock_day",
        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("boom")),
    )

    run._prefetch_symbol_ohlcv(
        session=None, symbol="2330", market_type="twse",
        start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31),
        traded_dates=set(),
    )

    out = capsys.readouterr().out
    assert "boom" in out
    assert "2025-07" in out


def test_probe_transport_failure_is_printed(monkeypatch, capsys) -> None:
    """探測市場別那條路徑原本吞掉所有例外且什麼都不印。"""
    monkeypatch.setattr(
        run, "fetch_twse_stock_day",
        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("twse down")),
    )
    monkeypatch.setattr(
        run, "fetch_tpex_stock_day",
        lambda session, stock_no, date: _tpex_month_df(16),
    )

    out = run._prefetch_symbol_ohlcv(
        session=None, symbol="6488", market_type=None,
        start=dt.date(2025, 6, 1), end=dt.date(2025, 6, 30),
        traded_dates=set(),
    )

    assert out.market_type == "tpex"
    assert "twse down" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _fetch_month_ohlcv 的月份過濾
# ---------------------------------------------------------------------------


def test_fetch_month_ohlcv_drops_rows_outside_the_month(monkeypatch) -> None:
    """月表回到別的月份時要過濾掉。

    TPEX 有強制月份驗證，TWSE 沒有；若不過濾，`_prefetch_symbol_ohlcv` 的
    `if month_rows:` 會誤判成「這個月抓到了」，讓真正缺料的月份逃過 MoneyDJ 交叉
    比對。
    """
    wrong_month = pd.DataFrame(
        [["114/08/05", "24,000,000", "30,000,000", "1250.00", "1260.00",
          "1245.00", "1255.00", "-5.00", "50,000", ""]],
        columns=["日期", "成交股數", "成交金額", "開盤價", "最高價", "最低價",
                 "收盤價", "漲跌價差", "成交筆數", "註記"],
    )
    monkeypatch.setattr(run, "fetch_twse_stock_day", lambda *a, **k: wrong_month)

    assert run._fetch_month_ohlcv(None, "2330", "twse", dt.date(2025, 7, 1)) == {}


def test_wrong_month_response_does_not_mask_a_missing_month(monkeypatch) -> None:
    """接上一個測試：過濾之後，回錯月份的月份仍然會被交叉比對抓出來。"""
    wrong_month = pd.DataFrame(
        [["114/08/05", "24,000,000", "30,000,000", "1250.00", "1260.00",
          "1245.00", "1255.00", "-5.00", "50,000", ""]],
        columns=["日期", "成交股數", "成交金額", "開盤價", "最高價", "最低價",
                 "收盤價", "漲跌價差", "成交筆數", "註記"],
    )
    monkeypatch.setattr(run, "fetch_twse_stock_day", lambda *a, **k: wrong_month)
    monkeypatch.setattr(
        run, "fetch_tpex_stock_day",
        lambda *a, **k: (_ for _ in ()).throw(DataUnavailableError("無資料")),
    )

    out = run._prefetch_symbol_ohlcv(
        session=None, symbol="2330", market_type="twse",
        start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31),
        traded_dates={dt.date(2025, 7, 15)},
    )

    assert out.by_date == {}
    assert out.failed_months == [dt.date(2025, 7, 1)]


def test_unknown_market_type_probes_twse_first(monkeypatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(
        run, "fetch_twse_stock_day",
        lambda *a, **k: order.append("twse") or _twse_month_df(15),
    )
    monkeypatch.setattr(
        run, "fetch_tpex_stock_day",
        lambda *a, **k: order.append("tpex") or pd.DataFrame(),
    )

    out = run._prefetch_symbol_ohlcv(
        session=None, symbol="2330", market_type=None,
        start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31),
        traded_dates=set(),
    )

    assert order == ["twse"]
    assert out.market_type == "twse"


def test_unknown_market_type_falls_back_to_tpex(monkeypatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(
        run, "fetch_twse_stock_day",
        lambda *a, **k: order.append("twse")
        or (_ for _ in ()).throw(DataUnavailableError("無資料")),
    )
    monkeypatch.setattr(
        run, "fetch_tpex_stock_day",
        lambda session, stock_no, date: order.append("tpex") or pd.DataFrame(
            [["114/07/16", "3,709", "1,184,624", "310.00", "326.50",
              "307.50", "322.50", "18.50", "5,095"]],
            columns=["日 期", "成交張數", "成交仟元", "開盤", "最高", "最低",
                     "收盤", "漲跌", "筆數"],
        ),
    )

    out = run._prefetch_symbol_ohlcv(
        session=None, symbol="6488", market_type=None,
        start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31),
        traded_dates=set(),
    )

    assert order == ["twse", "tpex"]
    assert out.market_type == "tpex"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
