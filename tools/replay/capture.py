"""對指定的套件版本跑一條抓取路徑，側錄所有上游呼叫與 DB 寫入。

同一份錄好的 tape 餵給重構前後兩個版本，就能證明「同樣的上游回應，兩邊產出相同」。

    # 1. 用基準版錄一次（真的打 API）
    python tools/replay/capture.py --src <base>/src --mode daily \
        --record --tape tape.json --out base.json

    # 2. 離線重放給兩個版本
    python tools/replay/capture.py --src <base>/src --mode daily --tape tape.json --out base.json
    python tools/replay/capture.py --src ./src     --mode daily --tape tape.json --out dev.json

    # 3. 比對
    python tools/replay/compare.py base.json dev.json base dev

側錄的是「呼叫順序 + 參數 + 完整回傳值」，所以呼叫次數、順序、參數、
抓回或 normalize 出來的資料，任一改變都看得出來。
"""
from __future__ import annotations

import argparse
import datetime as dt
import functools
import io
import json
import os
import sys
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 各模式要側錄的函式。以 run 模組的命名空間為準——run.py 用
# `from .sources import fetch_x`，包 sources.fetch_x 攔不到實際呼叫。
WRAP = {
    "daily": ("fetch_", "prepare_", "expand_", "find_"),
    "dahu": ("fetch_tdcc", "prepare_tdcc"),
    "backfill-stocks": (
        "fetch_twse_stock_day", "fetch_tpex_stock_day", "expand_twse_stock_day",
        "expand_tpex_stock_day", "_fetch_month_ohlcv", "_prefetch_symbol_ohlcv",
        "_month_starts", "_month_end", "fetch_moneydj_margin",
        "fetch_moneydj_holding_pct", "prepare_moneydj_margin",
        "prepare_moneydj_holding_pct", "prepare_moneydj_insti",
        "_fetch_disposition", "_prefetch_margin_cache",
    ),
}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--src", required=True, help="套件的 src 目錄（可指向 git worktree）")
    p.add_argument("--mode", required=True, choices=sorted(WRAP))
    p.add_argument("--tape", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--record", action="store_true",
                   help="真的打 API 並寫進 tape；不加則完全離線重放")
    p.add_argument("--stocks", default=os.path.join(HERE, "stocks.json"))
    p.add_argument("--days", default=os.path.join(HERE, "days.json"),
                   help="daily 模式要跑的日期清單")
    p.add_argument("--start", default="2025-06-01", help="dahu / backfill-stocks 的區間起")
    p.add_argument("--end", default="2025-08-31")
    p.add_argument("--today", default=None, help="視為「今天」的日期，預設為系統今天")
    args = p.parse_args()

    sys.path.insert(0, args.src)
    import pandas as pd
    import httptape
    from serialize import arg_summary, enc
    from tw_stock_rawdata import run
    from tw_stock_rawdata.config import AppConfig

    stocks = json.load(open(args.stocks, encoding="utf-8"))
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    config = AppConfig(database_url="replay://not-a-real-db", use_db=True)

    calls: list = []
    prefixes = WRAP[args.mode]
    for name in sorted(n for n in dir(run)
                       if n.startswith(prefixes) and callable(getattr(run, n))):
        orig = getattr(run, name)

        def make(name, orig):
            @functools.wraps(orig)
            def wrapper(*a, **k):
                rec = {"fn": name, "args": arg_summary(a, k)}
                try:
                    r = orig(*a, **k)
                except Exception as e:
                    rec["raised"] = f"{type(e).__name__}: {str(e)[:150]}"
                    calls.append(rec)
                    raise
                rec["ret"] = enc(r)
                calls.append(rec)
                return r
            return wrapper
        setattr(run, name, make(name, orig))

    if args.record:
        import requests
        real = requests.Session()
        real.headers.update({"User-Agent": "tw-stock-rawdata/0.1"})
        session = httptape.RecordingSession(real, args.tape)
    else:
        session = httptape.ReplaySession(args.tape)

    # DB 一律攔掉：比對的是「要寫什麼」，不是「寫進去沒有」。
    writes: list = []
    run.upsert_daily_raw = lambda url, date, df: writes.append(
        {"kind": "daily_raw", "date": date.isoformat(), "data": enc(df)})
    run.upsert_market_daily = lambda url, date, data: writes.append(
        {"kind": "market_daily", "date": date.isoformat(), "data": enc(data)})
    run.upsert_holder_percent = lambda url, date, rows: (writes.append(
        {"kind": "holder_percent", "date": date.isoformat(), "data": enc(rows)}), len(rows))[1]
    run.correct_prev_margin_balance = lambda *a, **k: None
    run.find_consensus_prev_trade_date = lambda *a, **k: None
    run.update_prev_day_margin_batch = lambda url, ups: len(ups)
    run._refresh_prev_day_margin = lambda *a, **k: None
    run.load_stock_names = lambda url: {s["symbol"]: s["name"] for s in stocks}
    run.load_market_types = lambda url: {s["symbol"]: s["market_type"] for s in stocks}
    run.get_enabled_stocks = lambda url: [
        (s["symbol"], s["name"], None, None, s["market_type"]) for s in stocks]
    run._get_issued_shares = lambda s, c: {}
    run.build_session = lambda *a, **k: session
    run.time.sleep = lambda _s: None          # 重試 backoff 在重放時沒有意義

    err = None
    buf = io.StringIO()
    per_day: dict = {}

    if args.mode == "daily":
        days = [dt.date.fromisoformat(d) for d in json.load(open(args.days, encoding="utf-8"))]
        holdings = pd.DataFrame(stocks)
        name_map = {s["symbol"]: s["name"] for s in stocks}
        for day in days:
            calls.clear()
            del writes[:]
            try:
                with redirect_stdout(buf):
                    run._run_for_date(session, day, holdings.copy(), set(), {}, config,
                                      today, skip_existing=False, name_map=name_map)
            except Exception as e:
                calls.append({"fn": "<TOP-LEVEL>", "raised": f"{type(e).__name__}: {str(e)[:150]}"})
            per_day[day.isoformat()] = {"calls": list(calls), "writes": list(writes)}
            print(f"[{day}] {len(calls)} 次呼叫, {len(writes)} 次寫入", flush=True)
    else:
        ns = argparse.Namespace(
            date=None, backfill_start=None, backfill_end=None, backfill_stocks=None,
            backfill_limits=False, backfill_disposition=False, update_shares=False,
            dahu=False, force=False, stocks=None, from_date=None, to_date=None)
        if args.mode == "dahu":
            ns.dahu = True
            ns.from_date, ns.to_date = args.start, args.end
            target = lambda: run._dahu_command(session, config, ns, today)
        else:
            ns.backfill_stocks = ",".join(s["symbol"] for s in stocks)
            ns.backfill_start, ns.backfill_end = args.start, args.end
            target = lambda: run._main_inner(config, ns, today, dt.date.fromisoformat(args.end))
        try:
            with redirect_stdout(buf):
                target()
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:250]}"
        per_day["<run>"] = {"calls": list(calls), "writes": list(writes)}
        print(f"{len(calls)} 次呼叫, {len(writes)} 次寫入, error={err}")

    out = {"mode": args.mode, "per_day": per_day, "error": err}
    if args.record:
        session.save()
        out["requests_made"] = session.n
        print(f"\n實際發出 {session.n} 次請求")
    else:
        out["tape_misses"] = session.misses
        print(f"\ntape 命中 {session.hits}，miss {len(session.misses)}")
        if session.misses:
            print("  ⚠ miss 代表兩個版本送出的請求集合不同，本身就是差異訊號")
    json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False)


if __name__ == "__main__":
    main()
