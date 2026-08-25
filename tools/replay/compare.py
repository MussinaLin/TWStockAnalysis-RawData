"""比對兩份 capture.py 輸出：呼叫序列、參數、回傳值、DB 寫入內容。

    python tools/replay/compare.py base.json dev.json base dev

退出碼 0 = 完全相同，1 = 有差異。
"""
from __future__ import annotations

import json
import sys
from collections import Counter


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    A = json.load(open(sys.argv[1], encoding="utf-8"))
    B = json.load(open(sys.argv[2], encoding="utf-8"))
    LA = sys.argv[3] if len(sys.argv) > 3 else "A"
    LB = sys.argv[4] if len(sys.argv) > 4 else "B"
    a, b = A["per_day"], B["per_day"]

    n_calls = n_arg = n_ret = n_seq = n_write = 0
    fn_stats: dict = {}
    diff_keys = set()

    for ma, lbl in ((A, LA), (B, LB)):
        if ma.get("tape_misses"):
            print(f"⚠ {lbl} 有 {len(ma['tape_misses'])} 次 tape miss"
                  f"（送出了對方沒送過的請求）")
            for m in ma["tape_misses"][:5]:
                print(f"    {m['method']} {m['url']} {m.get('params') or m.get('data')}")

    if A.get("error") != B.get("error"):
        print(f"⚠ 頂層例外不同: {LA}={A.get('error')!r}  {LB}={B.get('error')!r}")

    for key in sorted(set(a) | set(b)):
        ra, rb = a.get(key, {}), b.get(key, {})
        ca, cb = ra.get("calls", []), rb.get("calls", [])
        seq_a, seq_b = [c["fn"] for c in ca], [c["fn"] for c in cb]
        if seq_a != seq_b:
            n_seq += 1
            diff_keys.add(key)
            print(f"\n═══ {key} 呼叫序列不同 ═══")
            print(f"  {LA} {len(seq_a)} 次 / {LB} {len(seq_b)} 次")
            da, db = Counter(seq_a), Counter(seq_b)
            for fn in sorted(set(da) | set(db)):
                if da[fn] != db[fn]:
                    print(f"    {fn:<34}{LA}={da[fn]}  {LB}={db[fn]}")
            continue
        for i, (x, y) in enumerate(zip(ca, cb)):
            n_calls += 1
            s = fn_stats.setdefault(x["fn"], {"n": 0, "diff": 0})
            s["n"] += 1
            if x.get("args") != y.get("args"):
                n_arg += 1
                s["diff"] += 1
                diff_keys.add(key)
                print(f"\n{key} #{i} {x['fn']} 參數不同")
                print(f"  {LA}: {json.dumps(x.get('args'), ensure_ascii=False)[:300]}")
                print(f"  {LB}: {json.dumps(y.get('args'), ensure_ascii=False)[:300]}")
            if x.get("ret") != y.get("ret") or x.get("raised") != y.get("raised"):
                n_ret += 1
                s["diff"] += 1
                diff_keys.add(key)
                print(f"\n{key} #{i} {x['fn']} 回傳不同")
                print(f"  {LA}: {json.dumps(x.get('ret', x.get('raised')), ensure_ascii=False)[:400]}")
                print(f"  {LB}: {json.dumps(y.get('ret', y.get('raised')), ensure_ascii=False)[:400]}")
        if ra.get("writes") != rb.get("writes"):
            n_write += 1
            diff_keys.add(key)
            wa, wb = ra.get("writes", []), rb.get("writes", [])
            print(f"\n{key} DB 寫入不同: {LA}={len(wa)} 筆  {LB}={len(wb)} 筆")
            for i, (x, y) in enumerate(zip(wa, wb)):
                if x != y:
                    print(f"  首個不同: #{i} kind={x.get('kind')} date={x.get('date')}")
                    break

    print(f"\n{'=' * 66}")
    if fn_stats:
        print(f"{'函式':<36}{'呼叫':>8}{'差異':>8}")
        for fn in sorted(fn_stats):
            s = fn_stats[fn]
            print(f"  {fn:<34}{s['n']:>8}{s['diff']:>8}")
        print("=" * 66)
    print(f"比對 {len(set(a) | set(b))} 個單位、{n_calls} 次呼叫")
    print(f"呼叫序列不同: {n_seq}   參數不同: {n_arg}   回傳不同: {n_ret}   寫入不同: {n_write}")
    ok = not diff_keys and not A.get("tape_misses") and not B.get("tape_misses")
    print("結果：完全相同" if ok else f"結果：{len(diff_keys)} 個單位有差異")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
