"""Unit tests: 大盤行情寫入 market_daily。

docs/refactor-plan.md T2。_fetch_and_upsert_market_daily 原本覆蓋率 2%
（58 行只有 1 行被執行），CC 16，且是**唯一**寫共用表 market_daily 的路徑
（CLAUDE.md 列為 --backfill-stocks 刻意不碰的表），完全沒有回歸保護。

核心不變量：各子來源各自獨立失敗，任一失敗不影響其他來源，也不阻止寫入
已取得的部分——搭配 upsert_market_daily 的 COALESCE，缺的欄位不會覆寫舊值。
子來源：TWSE 大盤 OHLC／成交金額／外資買賣超／融資、期交所台指期三大法人未平倉、
期交所台指 VIX、TWSE 上市當沖統計。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
import requests

from tw_stock_rawdata import run
from tw_stock_rawdata.config import AppConfig
from tw_stock_rawdata.sources import DataUnavailableError

CONFIG = AppConfig(database_url="postgres://x", use_db=True)
DATE = dt.date(2026, 8, 21)

OHLC = {"taiex_open": 24000.0, "taiex_high": 24200.0,
        "taiex_low": 23900.0, "taiex_close": 24100.0}


DAY_TRADE = {
    "day_trade_shares": 4460672000, "day_trade_shares_pct": 0.261,
    "day_trade_buy_amount": 322561954550, "day_trade_buy_amount_pct": 0.3448,
    "day_trade_sell_amount": 322720245100, "day_trade_sell_amount_pct": 0.345,
}


def _taifex_df(date_str: str = "2026/08/21") -> pd.DataFrame:
    """fetch_taifex_txf_institutional 的原始字串表（只放 prepare 會讀的欄）。"""
    rows = [("自營商", "1159", "9455127"), ("投信", "92406", "754751439"),
            ("外資及陸資", "-96787", "-790605277")]
    return pd.DataFrame(
        [{"日期": date_str, "商品名稱": "臺股期貨", "身份別": who,
          "多空未平倉口數淨額": lots, "多空未平倉契約金額淨額(千元)": amount}
         for who, lots, amount in rows]
    )


_MINI_INST = {  # commodity_id -> (商品名稱, [(身份別, 多方未平倉口數, 空方未平倉口數)])
    "MXF": ("小型臺指期貨", [("自營商", "7578", "5227"), ("投信", "90", "168"), ("外資及陸資", "5010", "3457")]),
    "TMF": ("微型臺指期貨", [("自營商", "2099", "34153"), ("投信", "0", "0"), ("外資及陸資", "12669", "7476")]),
}
_MINI_REPORT_OI = {"MTX": "33055", "TMF": "75681"}
MINI_EXPECTED = {
    "mtx_market_oi": 33055, "mtx_inst_long_oi": 12678, "mtx_inst_short_oi": 8852,
    "tmf_market_oi": 75681, "tmf_inst_long_oi": 14768, "tmf_inst_short_oi": 41629,
}


def _mini_inst_df(commodity_id: str, date_str: str = "2026/08/21") -> pd.DataFrame:
    """fetch_taifex_fut_institutional 的原始字串表（只放 prepare 會讀的欄）。"""
    product, rows = _MINI_INST[commodity_id]
    return pd.DataFrame(
        [{"日期": date_str, "商品名稱": product, "身份別": who,
          "多方未平倉口數": long_, "空方未平倉口數": short}
         for who, long_, short in rows]
    )


def _mini_report_df(commodity_id: str, date_str: str = "2026/08/21") -> pd.DataFrame:
    """fetch_taifex_fut_daily_report 的原始字串表：一般單式 1 列 + 盤後 1 列。"""
    return pd.DataFrame([
        {"交易日期": date_str, "契約": commodity_id, "到期月份(週別)": "202609",
         "未沖銷契約數": _MINI_REPORT_OI[commodity_id], "交易時段": "一般"},
        {"交易日期": date_str, "契約": commodity_id, "到期月份(週別)": "202609",
         "未沖銷契約數": "-", "交易時段": "盤後"},
    ])


@pytest.fixture
def wiring(monkeypatch):
    calls = {"upserts": [], "corrections": [], "taifex": [], "mini_inst": [], "mini_report": []}
    monkeypatch.setattr(run, "fetch_twse_taiex_ohlc", lambda s, d: {DATE: dict(OHLC)})
    monkeypatch.setattr(run, "fetch_twse_market_volume", lambda s, d: {DATE: 123456})
    monkeypatch.setattr(run, "fetch_twse_foreign_net", lambda s, d: -5000)
    monkeypatch.setattr(
        run, "fetch_twse_market_margin",
        lambda s, d: {"margin_balance": 300, "prev_margin_balance": 280},
    )

    def fake_taifex(session, start, end):
        calls["taifex"].append((start, end))
        return _taifex_df()

    monkeypatch.setattr(run, "fetch_taifex_txf_institutional", fake_taifex)

    def fake_mini_inst(session, start, end, commodity_id):
        calls["mini_inst"].append((commodity_id, start, end))
        return _mini_inst_df(commodity_id)

    def fake_mini_report(session, commodity_id, start, end):
        calls["mini_report"].append((commodity_id, start, end))
        return _mini_report_df(commodity_id)

    monkeypatch.setattr(run, "fetch_taifex_fut_institutional", fake_mini_inst)
    monkeypatch.setattr(run, "fetch_taifex_fut_daily_report", fake_mini_report)
    monkeypatch.setattr(run, "fetch_taifex_vix_close", lambda s, d: 52.13)
    monkeypatch.setattr(run, "fetch_twse_day_trade", lambda s, d: dict(DAY_TRADE))

    def fake_correct(url, date, prev_balance):
        calls["corrections"].append((date, prev_balance))
        return None

    monkeypatch.setattr(run, "correct_prev_margin_balance", fake_correct)

    def fake_upsert(url, date, data):
        calls["upserts"].append((date, dict(data)))

    monkeypatch.setattr(run, "upsert_market_daily", fake_upsert)
    return calls


def _written(calls) -> dict:
    assert len(calls["upserts"]) == 1
    return calls["upserts"][0][1]


class TestHappyPath:
    def test_all_sources_merged_into_one_upsert(self, wiring) -> None:
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        data = _written(wiring)
        assert data["taiex_close"] == 24100.0
        assert data["total_volume"] == 123456
        assert data["foreign_net"] == -5000
        assert data["margin_balance"] == 300

    def test_prev_margin_balance_is_not_written_to_market_daily(self, wiring) -> None:
        """prev_margin_balance 只用來校正 D-1，不是 market_daily 的欄位。"""
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        assert "prev_margin_balance" not in _written(wiring)

    def test_prev_margin_balance_triggers_correction(self, wiring) -> None:
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        assert wiring["corrections"] == [(DATE, 280)]

    def test_futures_oi_merged_into_same_upsert(self, wiring) -> None:
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        data = _written(wiring)
        assert data["txf_foreign_oi_net"] == -96787
        assert data["txf_total_oi_net"] == 1159 + 92406 - 96787
        assert data["txf_foreign_oi_net_amount"] == -790_605_277_000

    def test_futures_oi_queries_only_the_target_date(self, wiring) -> None:
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        assert wiring["taifex"] == [(DATE, DATE)]

    def test_futures_oi_for_other_date_is_not_used(self, monkeypatch, wiring) -> None:
        monkeypatch.setattr(
            run, "fetch_taifex_txf_institutional", lambda s, a, b: _taifex_df("2026/08/20")
        )
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        assert "txf_foreign_oi_net" not in _written(wiring)

    def test_date_not_in_ohlc_map_is_skipped(self, monkeypatch, capsys, wiring) -> None:
        """月表回傳的是整月，請求日不在其中時不可誤用別天的資料，且要印 log。"""
        other = dt.date(2026, 8, 20)
        monkeypatch.setattr(run, "fetch_twse_taiex_ohlc", lambda s, d: {other: dict(OHLC)})
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        assert "taiex_close" not in _written(wiring)
        assert "大盤 OHLC" in capsys.readouterr().out

    def test_date_not_in_volume_map_is_logged(self, monkeypatch, capsys, wiring) -> None:
        monkeypatch.setattr(run, "fetch_twse_market_volume", lambda s, d: {dt.date(2026, 8, 20): 1})
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        assert "total_volume" not in _written(wiring)
        assert "大盤成交金額" in capsys.readouterr().out

    def test_vix_merged_into_same_upsert(self, wiring) -> None:
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        assert _written(wiring)["tw_vix_close"] == 52.13

    def test_vix_failure_is_logged_and_keeps_others(self, monkeypatch, capsys, wiring) -> None:
        def fail(session, date):
            raise DataUnavailableError("x")

        monkeypatch.setattr(run, "fetch_taifex_vix_close", fail)
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        data = _written(wiring)
        assert "tw_vix_close" not in data
        assert data["taiex_close"] == 24100.0
        assert "台指 VIX 取得失敗：x" in capsys.readouterr().out

    def test_day_trade_merged_into_same_upsert(self, wiring) -> None:
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        data = _written(wiring)
        for col, value in DAY_TRADE.items():
            assert data[col] == value

    def test_day_trade_failure_is_logged_and_keeps_others(
        self, monkeypatch, capsys, wiring
    ) -> None:
        def fail(session, date):
            raise DataUnavailableError("TWTB4U：很抱歉，沒有符合條件的資料!")

        monkeypatch.setattr(run, "fetch_twse_day_trade", fail)
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        data = _written(wiring)
        assert "day_trade_shares" not in data
        assert data["tw_vix_close"] == 52.13
        assert "大盤當沖統計取得失敗：TWTB4U：很抱歉" in capsys.readouterr().out

    def test_happy_path_logs_no_failure(self, capsys, wiring) -> None:
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        assert "失敗" not in capsys.readouterr().out


class TestPartialFailureIsolation:
    """各子來源各自獨立失敗，不影響其他來源，也不阻止寫入已取得的部分。"""

    @pytest.mark.parametrize(
        "attr,exc,lost_key,kept_key",
        [
            ("fetch_twse_taiex_ohlc", DataUnavailableError("x"), "taiex_close", "total_volume"),
            ("fetch_twse_market_volume", DataUnavailableError("x"), "total_volume", "taiex_close"),
            ("fetch_twse_foreign_net", requests.RequestException("x"), "foreign_net", "taiex_close"),
            ("fetch_twse_market_margin", requests.RequestException("x"), "margin_balance", "taiex_close"),
            ("fetch_twse_foreign_net", DataUnavailableError("x"), "foreign_net", "taiex_close"),
            ("fetch_twse_market_margin", DataUnavailableError("x"), "margin_balance", "taiex_close"),
            ("fetch_taifex_txf_institutional", DataUnavailableError("查無資料"),
             "txf_foreign_oi_net", "taiex_close"),
            ("fetch_taifex_txf_institutional", requests.RequestException("x"),
             "txf_foreign_oi_net", "taiex_close"),
            ("fetch_twse_taiex_ohlc", DataUnavailableError("x"), "taiex_close", "txf_foreign_oi_net"),
        ],
    )
    def test_one_source_failure_keeps_the_rest(
        self, monkeypatch, wiring, attr, exc, lost_key, kept_key
    ) -> None:
        monkeypatch.setattr(run, attr, lambda *a, **k: (_ for _ in ()).throw(exc))
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        data = _written(wiring)
        assert lost_key not in data
        assert kept_key in data

    def test_all_sources_fail_writes_nothing(self, monkeypatch, wiring) -> None:
        for attr, exc in [
            ("fetch_twse_taiex_ohlc", DataUnavailableError("x")),
            ("fetch_twse_market_volume", DataUnavailableError("x")),
            ("fetch_twse_foreign_net", DataUnavailableError("x")),
            ("fetch_twse_market_margin", DataUnavailableError("x")),
            ("fetch_taifex_txf_institutional", DataUnavailableError("x")),
            ("fetch_taifex_fut_institutional", DataUnavailableError("x")),
            ("fetch_taifex_fut_daily_report", DataUnavailableError("x")),
            ("fetch_taifex_vix_close", DataUnavailableError("x")),
            ("fetch_twse_day_trade", DataUnavailableError("x")),
        ]:
            # exc 綁進預設參數：直接閉包會晚綁定，每個來源都丟最後一個例外。
            monkeypatch.setattr(
                run, attr, lambda *a, exc=exc, **k: (_ for _ in ()).throw(exc)
            )
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        assert wiring["upserts"] == []

    def test_foreign_net_unavailable_is_logged_with_reason(self, monkeypatch, capsys, wiring) -> None:
        """BFI82U 失敗原本靜默回 None；限流字串要印出來才查得到。"""
        def fail(s, d):
            raise DataUnavailableError("BFI82U：很抱歉，沒有符合條件的資料!")
        monkeypatch.setattr(run, "fetch_twse_foreign_net", fail)
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        assert "foreign_net" not in _written(wiring)
        out = capsys.readouterr().out
        assert "大盤外資買賣超取得失敗" in out and "很抱歉" in out

    def test_margin_unavailable_is_logged_and_skips_correction(
        self, monkeypatch, capsys, wiring
    ) -> None:
        """MI_MARGN 失敗會連帶跳過 D-1 校正，必須留下 log。"""
        def fail(s, d):
            raise DataUnavailableError("MI_MARGN：很抱歉，沒有符合條件的資料!")
        monkeypatch.setattr(run, "fetch_twse_market_margin", fail)
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        assert wiring["corrections"] == []
        out = capsys.readouterr().out
        assert "大盤融資餘額取得失敗" in out and "很抱歉" in out


    def test_futures_oi_format_change_does_not_block_others(self, monkeypatch, wiring) -> None:
        """期交所表頭改版（prepare 丟 DataUnavailableError）同樣只影響期貨欄。"""
        monkeypatch.setattr(
            run, "fetch_taifex_txf_institutional",
            lambda s, a, b: _taifex_df().rename(columns={"身份別": "身分別"}),
        )
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        data = _written(wiring)
        assert "txf_foreign_oi_net" not in data
        assert data["taiex_close"] == 24100.0


class TestCorrectionFailureIsContained:
    def test_correction_exception_does_not_block_upsert(self, monkeypatch, wiring) -> None:
        """D-1 校正是附加動作，失敗不可拖累當日大盤資料的寫入。"""
        monkeypatch.setattr(
            run, "correct_prev_margin_balance",
            lambda *a: (_ for _ in ()).throw(RuntimeError("DB 連線中斷")),
        )
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        assert _written(wiring)["taiex_close"] == 24100.0

    def test_correction_result_is_reported(self, monkeypatch, capsys, wiring) -> None:
        monkeypatch.setattr(
            run, "correct_prev_margin_balance",
            lambda url, d, bal: (dt.date(2026, 8, 20), 270, 280, 10, 20),
        )
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        out = capsys.readouterr().out
        assert "2026-08-20" in out and "TWSE 事後修正" in out

    def test_correction_handles_null_old_values(self, monkeypatch, capsys, wiring) -> None:
        """舊值為 None 時印 NULL，不可因格式化 None 而丟例外。"""
        monkeypatch.setattr(
            run, "correct_prev_margin_balance",
            lambda url, d, bal: (dt.date(2026, 8, 20), None, 280, None, 20),
        )
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        assert "NULL" in capsys.readouterr().out


class TestMiniFuturesOi:
    """小台／微台：每商品兩個來源（法人、行情）各自獨立失敗，併入同一次 upsert。"""

    def test_mini_futures_oi_merged_into_same_upsert(self, wiring) -> None:
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        data = _written(wiring)
        for key, value in MINI_EXPECTED.items():
            assert data[key] == value
        assert data["txf_foreign_oi_net"] == -96787

    def test_mini_futures_query_target_date_and_commodity_ids(self, wiring) -> None:
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        assert wiring["mini_inst"] == [("MXF", DATE, DATE), ("TMF", DATE, DATE)]
        assert wiring["mini_report"] == [("MTX", DATE, DATE), ("TMF", DATE, DATE)]

    def test_mini_report_failure_keeps_other_columns(self, monkeypatch, capsys, wiring) -> None:
        def report(session, commodity_id, start, end):
            if commodity_id == "TMF":
                raise requests.RequestException("520")
            return _mini_report_df(commodity_id)

        monkeypatch.setattr(run, "fetch_taifex_fut_daily_report", report)
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        data = _written(wiring)
        assert "tmf_market_oi" not in data
        for key in MINI_EXPECTED.keys() - {"tmf_market_oi"}:
            assert data[key] == MINI_EXPECTED[key]
        assert "txf_foreign_oi_net" in data and data["taiex_close"] == 24100.0
        assert "微台全市場未平倉取得失敗" in capsys.readouterr().out

    def test_mini_inst_failure_keeps_other_columns(self, monkeypatch, capsys, wiring) -> None:
        def inst(session, start, end, commodity_id):
            if commodity_id == "MXF":
                raise DataUnavailableError("查無資料")
            return _mini_inst_df(commodity_id)

        monkeypatch.setattr(run, "fetch_taifex_fut_institutional", inst)
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        data = _written(wiring)
        assert "mtx_inst_long_oi" not in data and "mtx_inst_short_oi" not in data
        assert data["mtx_market_oi"] == 33055 and data["tmf_inst_long_oi"] == 14768
        out = capsys.readouterr().out
        assert "小台三大法人未平倉取得失敗" in out and "查無資料" in out

    def test_mini_report_without_target_date_is_logged(self, monkeypatch, capsys, wiring) -> None:
        """休市日 futDataDown 回只有表頭（空表）→ 沒有該日：不寫、印 log。"""
        monkeypatch.setattr(
            run, "fetch_taifex_fut_daily_report",
            lambda s, c, a, b: _mini_report_df(c).iloc[0:0],
        )
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        data = _written(wiring)
        assert "mtx_market_oi" not in data and "tmf_market_oi" not in data
        assert data["mtx_inst_long_oi"] == 12678
        assert "沒有 2026-08-21" in capsys.readouterr().out

    def test_mini_format_change_does_not_block_others(self, monkeypatch, wiring) -> None:
        monkeypatch.setattr(
            run, "fetch_taifex_fut_institutional",
            lambda s, a, b, c: _mini_inst_df(c).rename(columns={"身份別": "身分別"}),
        )
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        data = _written(wiring)
        assert not any(k.endswith("_inst_long_oi") or k.endswith("_inst_short_oi") for k in data)
        assert data["mtx_market_oi"] == 33055 and data["taiex_close"] == 24100.0
