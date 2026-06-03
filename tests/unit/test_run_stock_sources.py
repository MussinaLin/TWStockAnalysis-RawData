"""Unit tests for _stock_sources_ok per-stock skip gate.

逐檔跳過規則：必要來源 = 三大法人（依市場別）。融資融券「不」納入必要
（個股可能不開放融資融券），故 margin 失敗不應讓個股被跳過。
"""

from __future__ import annotations

import pytest

from tw_stock_rawdata import run


@pytest.mark.parametrize(
    ("is_tpex", "twse_insti_ok", "tpex_insti_ok", "expected"),
    [
        # TWSE 個股：看 twse_insti_ok
        (False, True, True, True),
        (False, True, False, True),   # TPEX 法人失敗不影響 TWSE 個股
        (False, False, True, False),  # 自己市場法人失敗 → 跳過
        (False, False, False, False),
        # TPEX 個股：看 tpex_insti_ok
        (True, True, True, True),
        (True, False, True, True),    # TWSE 法人失敗不影響 TPEX 個股
        (True, True, False, False),   # 自己市場法人失敗 → 跳過
        (True, False, False, False),
    ],
)
def test_stock_sources_ok_insti_by_market(
    is_tpex: bool, twse_insti_ok: bool, tpex_insti_ok: bool, expected: bool
) -> None:
    assert (
        run._stock_sources_ok(
            is_tpex=is_tpex,
            twse_insti_ok=twse_insti_ok,
            tpex_insti_ok=tpex_insti_ok,
        )
        is expected
    )


def test_margin_is_not_a_blocking_source() -> None:
    """融資融券不是 _stock_sources_ok 的參數：margin 狀態完全不影響該檔是否寫入。
    （簽章僅依賴三大法人，這條測試鎖住「margin 非必要」的設計。）"""
    import inspect

    params = set(inspect.signature(run._stock_sources_ok).parameters)
    assert params == {"is_tpex", "twse_insti_ok", "tpex_insti_ok"}
    assert not any("margin" in p for p in params)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
