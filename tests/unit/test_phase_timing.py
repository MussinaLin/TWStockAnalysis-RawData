"""Unit tests: _phase 階段計時輸出（無網路、無 DB）。

需求背景：Railway 上「Starting Container」到第一行進度 log 之間有數分鐘空白，
但每日流程在逐檔迴圈之前還串了 DB 連線、schema、休市檢查與多組整批抓取，
全部不印任何東西，無法歸因。_phase 讓每個階段的起訖與耗時可見。

- 進入時就印「開始」：階段若卡住不返回，至少看得出卡在哪一段。
- 離開時印「完成」與耗時秒數。
- 階段拋例外時印「失敗」與耗時，並原樣往外拋（不吞例外）。
"""

from __future__ import annotations

import pytest

from tw_stock_rawdata import run


@pytest.fixture
def fake_clock(monkeypatch):
    """把 run.time.monotonic 換成可控時鐘，回傳推進用的 callable。"""
    state = {"now": 100.0}
    monkeypatch.setattr(run.time, "monotonic", lambda: state["now"])

    def advance(seconds: float) -> None:
        state["now"] += seconds

    return advance


class TestPhaseTiming:
    def test_prints_start_on_enter(self, capsys, fake_clock):
        """進入階段時立刻印出「開始」，讓卡住的階段可被定位。"""
        with run._phase("TWSE 整批"):
            captured_inside = capsys.readouterr().out
        assert "TWSE 整批" in captured_inside
        assert "開始" in captured_inside

    def test_prints_elapsed_on_exit(self, capsys, fake_clock):
        """離開階段時印出耗時秒數。"""
        with run._phase("TPEX 整批"):
            fake_clock(12.34)
        out = capsys.readouterr().out
        assert "TPEX 整批" in out
        assert "完成" in out
        assert "12.3s" in out

    def test_reports_elapsed_when_body_raises(self, capsys, fake_clock):
        """階段失敗時仍要印出耗時——最貴的階段往往正是失敗重試的那個。"""
        with pytest.raises(ValueError):
            with run._phase("融資融券整批"):
                fake_clock(56.0)
                raise ValueError("boom")
        out = capsys.readouterr().out
        assert "融資融券整批" in out
        assert "失敗" in out
        assert "56.0s" in out

    def test_does_not_swallow_exception(self, fake_clock):
        """例外必須原樣往外拋，不能被計時包裝吞掉。"""
        with pytest.raises(KeyError, match="k"):
            with run._phase("x"):
                raise KeyError("k")

    def test_flushes_output(self, monkeypatch, fake_clock):
        """輸出必須 flush——這個功能存在的唯一理由就是對抗 stdout 緩衝。"""
        flushes: list[bool] = []
        real_print = print

        def spy_print(*args, **kwargs):
            flushes.append(kwargs.get("flush", False))
            real_print(*args, **kwargs)

        monkeypatch.setattr("builtins.print", spy_print)
        with run._phase("y"):
            pass
        assert flushes, "階段計時應有輸出"
        assert all(flushes), "階段計時的每一行輸出都必須 flush=True"
