#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""budget.py 单独测（不用装 AstrBot）：python tests/test_budget.py [插件目录 默认上一层]"""

import calendar
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

d = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
spec = importlib.util.spec_from_file_location("budget", d / "core" / "budget.py")
budget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget)
Budget = budget.Budget
RES = []


def check(c, m, why=""):
    RES.append((bool(c), m))
    print(
        ("  [过]   " if c else "  [没过] ")
        + m
        + ("" if c or not why else f"\n           {str(why)[:600]}")
    )


def utc(y, mo, da, h, mi=0, s=0):
    return float(calendar.timegm((y, mo, da, h, mi, s, 0, 0, 0)))


TUE_OFF = utc(2026, 10, 27, 5)  # 星期二 UTC 05:00：空闲
TUE_PEAK = utc(2026, 10, 27, 2)  # 星期二 UTC 02:00：高峰
SAT_2 = utc(2026, 10, 31, 2)  # 星期六 UTC 02:00：周末不算高峰


def spend_to(b, usd, t):
    """往流水里记一笔正好 usd 美元的输出（按那个时刻的价格：高峰时段 token 少一半）"""
    out = round(usd / b.cost(0, 0, 1_000_000, t)[2] * 1e6)
    b.record(0, 0, out, t)


print("[1 曲线：跟原版说明里的例子对得上（half 0.3 / shape 0.6 / floor 0.1）]")
b = Budget({"half_usd": 0.3, "shape": 0.6, "floor": 0.1})
want = {0.05: 77, 0.1: 69, 0.3: 55, 0.6: 46, 1: 39, 2: 32, 5: 24}
got = {u: round(b.curve(u) * 100) for u in want}
check(
    got == want,
    "花 $0.05 → 77%  $0.1 → 69%  $0.3 → 55%  $0.6 → 46%  $1 → 39%  $2 → 32%  $5 → 24%",
    got,
)
check(b.curve(0) == 1.0 and b.curve(-3) == 1.0, "没花钱：100%")
check(
    abs(b.curve(1e9) - 0.1) < 0.01 and b.curve(1e300) >= 0.1,
    "花再多也有底线 10%（不会溢出报错）",
    b.curve(1e300),
)
check(
    Budget().curve(0.45) == 0.55 and Budget({"half_usd": 0}).curve(5) == 1.0,
    "插件默认 half 0.45：花 $0.45 → 55%；half 填 0 = 不省",
)
check(
    Budget({"floor": 5, "shape": -1}).cfg["floor"] == 1.0
    and Budget({"shape": -1}).cfg["shape"] > 0,
    "配置填错（底线 >1、形状 ≤0）：夹回合理范围",
)

print("\n[2 认 token 用量]")
pu = budget.parse_usage
check(
    pu(
        {
            "prompt_tokens": 1000,
            "prompt_cache_hit_tokens": 800,
            "prompt_cache_miss_tokens": 200,
            "completion_tokens": 50,
        }
    )
    == (200, 800, 50),
    "DeepSeek：prompt_cache_hit_tokens / prompt_cache_miss_tokens",
)
check(
    pu({"prompt_tokens": 1000, "prompt_cache_hit_tokens": 600, "completion_tokens": 5})
    == (400, 600, 5),
    "DeepSeek 只给了命中数：没命中 = 输入 - 命中",
)
check(
    pu(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 300},
        }
    )
    == (700, 300, 50),
    "OpenAI：prompt_tokens_details.cached_tokens",
)
check(
    pu({"prompt_tokens": 1000, "completion_tokens": 50}) == (1000, 0, 50)
    and pu(
        {
            "prompt_tokens": 10,
            "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": 99},
        }
    )
    == (0, 10, 1),
    "OpenAI 没给缓存数：全算没命中；缓存数比输入还多（坏数据）：夹住",
)
check(
    pu(SimpleNamespace(input_other=10, input_cached=20, output=5)) == (10, 20, 5),
    "AstrBot 的 TokenUsage（input_other / input_cached / output）",
)


class FakePydantic:  # 像 openai SDK 的 CompletionUsage：DeepSeek 多给的字段在 model_extra 里
    prompt_tokens = 120
    completion_tokens = 30
    prompt_tokens_details = None
    model_extra = {"prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 20}


check(
    pu(FakePydantic()) == (20, 100, 30),
    "openai SDK 的对象：从 model_extra 里读到 DeepSeek 的命中数",
)
check(
    all(pu(x) is None for x in (None, {}, "abc", 5, SimpleNamespace(foo=1))),
    "认不出的：None（不记）",
)

print("\n[3 价格 + 峰谷价]")
b = Budget()
um, uh, uo = b.cost(1_000_000, 1_000_000, 1_000_000, TUE_OFF)
check(
    abs(um - 0.15) < 1e-9 and abs(uh - 0.003) < 1e-9 and abs(uo - 0.60) < 1e-9,
    "默认价格（每百万）：没命中 $0.15、命中 $0.003、输出 $0.60",
    (um, uh, uo),
)
check(
    abs(sum(b.cost(1_000_000, 0, 1_000_000, TUE_PEAK)) - 1.5) < 1e-9, "高峰时段价格翻倍"
)
peak = {
    h: b.is_peak(utc(2026, 10, 27, h, m))
    for h, m in (
        (0, 59),
        (1, 0),
        (3, 59),
        (4, 0),
        (5, 30),
        (6, 0),
        (9, 59),
        (10, 0),
        (12, 0),
    )
}
check(
    peak
    == {
        0: False,
        1: True,
        3: True,
        4: False,
        5: False,
        6: True,
        9: True,
        10: False,
        12: False,
    },
    "工作日 UTC 01:00–04:00、06:00–10:00 是高峰（前闭后开）",
    peak,
)
check(not b.is_peak(SAT_2) and not b.is_peak(utc(2026, 11, 1, 7)), "周六、周日不算高峰")
check(
    not Budget({"peak_free_days": ["2026-10-27"]}).is_peak(TUE_PEAK)
    and Budget({"peak_free_days": ["2026-10-28"]}).is_peak(TUE_PEAK),
    "填了节假日那天全天原价",
)
check(
    not Budget({"peak_enable": False}).is_peak(TUE_PEAK)
    and abs(sum(Budget({"peak_enable": False}).cost(0, 0, 1e6, TUE_PEAK)) - 0.6) < 1e-9,
    "关掉峰谷价：一直原价",
)
b2 = Budget({"peak_hours_utc": "2:30-3，坏的, 23-25", "peak_mult": 3})
check(
    b2._peak == [(2.5, 3.0)]
    and b2.is_peak(utc(2026, 10, 27, 2, 45))
    and not b2.is_peak(TUE_PEAK)
    and abs(sum(b2.cost(0, 0, 1e6, utc(2026, 10, 27, 2, 45))) - 1.8) < 1e-9,
    "自己填高峰钟点和倍数；写错的跳过",
    b2._peak,
)
check(
    "×2" in b.peak_text(TUE_PEAK)
    and "04:00 结束" in b.peak_text(TUE_PEAK)
    and "原价" in b.peak_text(TUE_OFF),
    "峰谷说明",
    (b.peak_text(TUE_PEAK), b.peak_text(TUE_OFF)),
)
b = Budget()
usd1 = b.record(1000, 9000, 200, TUE_OFF)
usd2 = b.record(1000, 9000, 200, TUE_PEAK + 86400)  # 星期三 02:00：高峰
check(
    abs(usd1 - (1000 * 0.15 + 9000 * 0.003 + 200 * 0.6) / 1e6) < 1e-12
    and abs(usd2 - 2 * usd1) < 1e-12,
    "记账：按当时是不是高峰算钱",
    (usd1, usd2),
)

print("\n[4 场合]")
pk = budget.pick_kind
check(
    pk("follow", False, False) == "follow"
    and pk(" Chat ", True, True) == "chat"
    and pk("extra", True, False) == "extra",
    "别的插件写了 fatfish_kind：就用它（私聊、被 @ 也一样）",
)
check(
    pk(None, True, False) == "private" and pk("", True, True) == "private",
    "没写：私聊 = private",
)
check(
    pk(None, False, True) == "direct" and pk("  ", False, True) == "direct",
    "没写：群里被 @ / 引用 / 唤醒 = direct",
)
check(
    pk(None, False, False) == "chat" and pk(123, False, False) == "chat",
    "没写、群里也没人找它（别的插件让它自己接）= chat",
)
b = Budget()
spend_to(b, 0.45, TUE_OFF)  # 意愿 55%
pr = b.probs(TUE_OFF)
check(
    abs(pr["private"] - 1) < 1e-9
    and abs(pr["direct"] - 0.55**0.1) < 1e-6
    and abs(pr["follow"] - 0.55**2.5) < 1e-6
    and abs(pr["chat"] - 0.55**3) < 1e-6
    and abs(pr["extra"] - 0.55**3) < 1e-6,
    "各场合的概率 = 意愿 ^ 次方（私聊 0 = 永远回）",
    {k: round(v, 3) for k, v in pr.items()},
)
check(b.power("什么鬼") == 1.0, "不认识的场合按 1 次方算")
d1 = b.decide("private", "c", "1", now=TUE_OFF, roll=0.999999)
check(d1["ok"], "私聊默认每条都回（掷出 0.999999 也回）", d1)

print("\n[5 管理员不限 / 没开]")
b = Budget()
spend_to(b, 1000, TUE_OFF)  # 花了一大堆：意愿到底线
dd = b.decide("chat", "c", "9", admin=True, now=TUE_OFF, roll=0.99)
check(dd["ok"] and dd["by"] == "admin", "管理员：不掷，回", dd)
dd = Budget({"free_users": "777, 888"}).decide(
    "extra", "c", "888", now=TUE_OFF, roll=0.99
)
check(dd["ok"] and dd["by"] == "admin", "「不限的人」也不掷", dd)
b.configure({"enable": False})
dd = b.decide("chat", "c", "1", now=TUE_OFF, roll=0.99)
check(dd["ok"] and dd["by"] == "off", "关掉省 token：只记账不拦", dd)
b.configure({"enable": True})
dd = b.decide("chat", "c", "1", now=TUE_OFF, roll=0.99)
check(
    not dd["ok"]
    and dd["by"] == "roll"
    and "掷出 0.99" in dd["why"]
    and "插话" in dd["why"],
    "开着：花了很多以后插话掷不过",
    dd,
)

print("\n[6 火力全开]")
st = b.set_full(2, "测试", TUE_OFF)
check(
    st["on"]
    and st["left"] == 7200
    and st["by"] == "测试"
    and b.full_text(st) == "还剩 2 小时 0 分钟",
    "开 2 小时",
    st,
)
oks = [b.decide(k, "c", "1", now=TUE_OFF + 60, roll=0.999)["by"] for k in budget.KINDS]
check(oks == ["full"] * 5, "开着的时候所有场合都不掷（冒泡/主动也不掷）", oks)
check(
    not b.full_state(TUE_OFF + 7201)["on"]
    and not b.decide("chat", "c", "1", now=TUE_OFF + 7201, roll=0.999)["ok"],
    "到点自动关，又开始掷了",
)
check(b.full is None, "过期的火力全开清掉了（不留在存档里）", b.full)
st = b.set_full(-1, "", TUE_OFF)
check(
    st["on"]
    and st["left"] == -1
    and b.full_state(TUE_OFF + 86400 * 365)["on"]
    and "一直" in b.full_text(st),
    "一直开：不会自己关",
    st,
)
st = b.set_full(0, "", TUE_OFF)
check(not st["on"] and b.full_text(st) == "没开" and b.full is None, "关掉")
check(
    budget.parse_hours("") == 3
    and budget.parse_hours("", 5) == 5
    and budget.parse_hours("一直") == -1
    and budget.parse_hours("6") == 6
    and budget.parse_hours("6小时") == 6
    and budget.parse_hours("1.5h") == 1.5
    and budget.parse_hours("30分钟") == 0.5
    and budget.parse_hours("30 分钟") == 0.5
    and budget.parse_hours("1000") == 72,
    "指令里的小时数：默认 / 一直 / 6 / 6小时 / 1.5h / 30分钟；最多 72",
)
check(
    all(budget.parse_hours(x) is None for x in ("abc", "0", "-3", "3天", "0分钟")),
    "看不懂的、0、负数：不认（回用法）",
)
check(
    b.full_text({"on": True, "left": 600}) == "还剩 10 分钟"
    and b.full_text({"on": True, "left": 3 * 3600 + 59}) == "还剩 3 小时 0 分钟",
    "剩余时间的说法",
)

print("\n[7 被跳过以后又来找：开平方根]")
b = Budget({"never_skip_sought": False})  # 这一节测掷骰子；v0.4.3 起有人找它默认不掷（见第 13 节）
spend_to(b, 0.45, TUE_OFF)  # 意愿 55%：接话 0.55^2.5 ≈ 22%
p0 = 0.55**2.5
d1 = b.decide("follow", "g1", "5", "小李", now=TUE_OFF, roll=0.3)
check(
    not d1["ok"] and abs(d1["p"] - p0) < 1e-6 and d1["boost"] == 0,
    f"第一次：{p0:.0%} 掷出 0.30 → 跳过",
    d1,
)
d2 = b.decide("follow", "g1", "5", "小李", now=TUE_OFF + 30, roll=0.5)
check(
    not d2["ok"]
    and abs(d2["p"] - p0**0.5) < 1e-6
    and d2["boost"] == 1
    and "开了 1 次平方根" in d2["why"],
    f"跳过 1 次后又来：开一次平方根 {p0**0.5:.0%}",
    d2,
)
d3 = b.decide("direct", "g1", "5", "小李", now=TUE_OFF + 60, roll=0.0)
check(
    d3["ok"] and abs(d3["p"] - (0.55**0.1) ** 0.25) < 1e-6 and d3["boost"] == 2,
    "跳过 2 次后 @ 它：开两次平方根（^1/4），回了",
    d3,
)
d4 = b.decide("follow", "g1", "5", "小李", now=TUE_OFF + 90, roll=0.5)
check(
    not d4["ok"] and abs(d4["p"] - p0) < 1e-6 and d4["boost"] == 0,
    "回了就清零：又从头算",
    d4,
)
d5 = b.decide("follow", "g1", "6", "老王", now=TUE_OFF + 100, roll=0.0)
d6 = b.decide("follow", "g2", "5", "小李", now=TUE_OFF + 100, roll=0.0)
check(d5["boost"] == 0 and d6["boost"] == 0, "换个人、换个群：不算", (d5, d6))
b.decide("chat", "g3", "7", "阿珍", now=TUE_OFF, roll=0.99)
d7 = b.decide("chat", "g3", "7", "阿珍", now=TUE_OFF + 10, roll=0.99)
check(
    d7["boost"] == 0 and len(b.skips["g3|7"]["ts"]) == 2,
    "插话（它自己想插嘴）不开方，但跳过照样记着",
    (d7, b.skips.get("g3|7")),
)
d8 = b.decide("direct", "g3", "7", "阿珍", now=TUE_OFF + 20, roll=0.0)
check(d8["boost"] == 2, "插话被跳过的人回头 @ 它：照样开方", d8)
b.decide("follow", "g4", "8", "小周", now=TUE_OFF, roll=0.99)
d9 = b.decide("follow", "g4", "8", "小周", now=TUE_OFF + 16 * 60, roll=0.99)
check(d9["boost"] == 0, "15 分钟以前被跳过的不算（skip_window_min）", d9)
nb = Budget({"skip_window_min": 0})
spend_to(nb, 0.45, TUE_OFF)
nb.decide("follow", "g", "1", now=TUE_OFF, roll=0.99)
check(
    nb.decide("follow", "g", "1", now=TUE_OFF + 1, roll=0.99)["boost"] == 0,
    "skip_window_min 填 0：不开方",
)
b.decide("follow", "g5", "9", "小吴", now=TUE_OFF, roll=0.99)
b.decide("follow", "g5", "9", "小吴", admin=True, now=TUE_OFF + 5)
check(
    b.decide("follow", "g5", "9", "小吴", now=TUE_OFF + 10, roll=0.99)["boost"] == 0,
    "不管怎么回的（管理员 / 火力全开），回了就清零",
)
sk = b.snapshot(TUE_OFF + 16 * 60)["skips"]
check(
    any(x["who"] == "小周" and x["today"] == 2 for x in sk)
    and any(x["who"] == "小李" and x["today"] == 3 for x in sk),
    "每个人今天被跳过几次（面板看）",
    sk,
)

print("\n[8 重新算]")
b = Budget()
spend_to(b, 1.0, TUE_OFF - 3600)
spend_to(b, 0.2, TUE_OFF)
check(
    abs(b.spent(TUE_OFF) - 1.2) < 1e-6, "重新算之前：窗口里一共 $1.2", b.spent(TUE_OFF)
)
r = b.reset_now("管理员", TUE_OFF + 10)  # 跟上一笔是同一分钟
check(
    r and b.spent(TUE_OFF + 10) < 1e-9 and b.curve(b.spent(TUE_OFF + 10)) == 1.0,
    "点了以后：从 0 算，意愿 100%",
    b.spent(TUE_OFF + 10),
)
spend_to(b, 0.05, TUE_OFF + 20)  # 同一分钟里又花了
spend_to(b, 0.1, TUE_OFF + 300)
check(
    abs(b.spent(TUE_OFF + 400) - 0.15) < 1e-6,
    "之后花的照算（点的那一分钟里之前花的减掉）",
    b.spent(TUE_OFF + 400),
)
snap = b.snapshot(TUE_OFF + 400)
check(
    abs(snap["spent_all"] - 1.35) < 1e-6 and snap["reset"]["by"] == "管理员",
    "面板：窗口里一共花的照样看得到",
    snap["spent_all"],
)
check(
    b.reset_state(TUE_OFF + 10 + 24 * 3600 + 1) is None,
    "过了统计窗口（24 小时）自动失效",
)
b.reset_clear()
check(abs(b.spent(TUE_OFF + 400) - 1.35) < 1e-6, "手动恢复按实际花费算")

print("\n[9 流水只留统计窗口里的]")
b = Budget()
for i in range(48):
    spend_to(b, 0.01, TUE_OFF - 1800 - i * 3600)  # 每个小时的半点花 $0.01
check(
    abs(b.spent(TUE_OFF) - 0.24) < 1e-6,
    "最近 24 小时：只算窗口里的 24 笔",
    b.spent(TUE_OFF),
)
b.trim(TUE_OFF)
check(
    len(b.ledger) <= 25 and min(b.ledger) * 60 >= TUE_OFF - 24 * 3600 - 60,
    f"老的流水删掉了（剩 {len(b.ledger)} 格）",
)
hr = b.hourly(TUE_OFF)
check(
    len(hr) == 24 and all(abs(x - 0.01) < 1e-6 for x in hr[:-1]) and hr[-1] == 0,
    "每小时花了多少（面板画小柱子；这个小时还没花）",
    hr[-3:],
)
b6 = Budget({"window_hours": 6})
for i in range(30):
    spend_to(b6, 0.01, TUE_OFF - 1800 - i * 3600)
b6.trim(TUE_OFF)
check(
    abs(b6.spent(TUE_OFF) - 0.06) < 1e-6 and len(b6.ledger) >= 24,
    "统计窗口 6 小时：曲线只算 6 小时，流水还是留 24 小时（面板要看）",
    (b6.spent(TUE_OFF), len(b6.ledger)),
)
for i in range(5000):
    b.record(10, 10, 10, TUE_OFF + i)
check(
    len(b.ledger) <= 24 * 60 + 2,
    f"一分钟一格：调用再多也不会无限涨（{len(b.ledger)} 格）",
)
bw = Budget({"window_hours": 100000})
for i in range(0, 30 * 86400, 1800):
    bw.record(10, 10, 10, TUE_OFF - 30 * 86400 + i)
check(
    bw.cfg["window_hours"] == 168
    and Budget({"window_hours": 0}).cfg["window_hours"] == 1
    and len(bw.ledger) <= 7 * 48 + 2,
    f"统计窗口最多 168 小时（7 天）、最少 1 小时：窗口填得再大，流水也只留 7 天（{len(bw.ledger)} 格）",
    (bw.cfg["window_hours"], len(bw.ledger)),
)

print("\n[10 存档]")
b = Budget()
spend_to(b, 0.3, TUE_OFF)
b.set_full(3, "x", TUE_OFF)
b.reset_now("y", TUE_OFF + 5)
spend_to(b, 0.45, TUE_OFF + 120)
b.decide("follow", "g1", "5", "小李", now=TUE_OFF + 130, roll=0.99)
raw = json.dumps(b.dump(), ensure_ascii=False)
b2 = Budget(state=json.loads(raw))
check(
    abs(b2.spent(TUE_OFF + 140) - b.spent(TUE_OFF + 140)) < 1e-9
    and b2.full_state(TUE_OFF + 140)["on"]
    and b2.reset_state(TUE_OFF + 140)
    and b2.skips == b.skips
    and b2.day == b.day,
    "存了再读回来：花费、火力全开、重新算、跳过记录、今天的数都在",
    (b2.spent(TUE_OFF + 140), b.spent(TUE_OFF + 140)),
)
b3 = Budget(
    state={
        "ledger": [
            [1, 2],
            "x",
            [5, "a", 0, 0, 0, 0, 0, 0],
            [int(TUE_OFF // 60), 0.1, 0, 0, 0, 0, 0, 1],
        ],
        "full": "坏的",
        "reset": 3,
        "day": [],
        "skips": {"a": 1},
        "recent": "x",
    }
)
check(
    abs(b3.spent(TUE_OFF) - 0.1) < 1e-9
    and b3.full is None
    and b3.reset is None
    and b3.skips == {}
    and b3.recent == [],
    "坏的存档：能用的读进来，坏的跳过，不报错",
    b3.dump(),
)
check(Budget(state="乱七八糟").spent(TUE_OFF) == 0, "存档不是 dict：当没有")
b4 = Budget(
    state={
        "skips": {
            "g|1": {
                "ts": ["x", None, TUE_OFF - 30],
                "n": "abc",
                "last": "zz",
                "name": 5,
            },
            "g|2": {"ts": 7, "n": 2},
        },
        "day": {
            "day": Budget._daykey(TUE_OFF),
            "pass": {"chat": "x", "direct": 2},
            "skip": {"chat": None},
            "admin": "?",
        },
    }
)
try:
    spend_to(b4, 0.45, TUE_OFF)
    d4, sn4, err4 = (
        b4.decide("follow", "g", "1", now=TUE_OFF, roll=0.0),
        b4.snapshot(TUE_OFF),
        None,
    )
except Exception as e:  # 修之前：TypeError —— 记账、掷骰子每次都出错（照常回 = 插件等于没装）、面板读不到
    d4, sn4, err4 = None, None, e
check(
    err4 is None
    and d4["ok"]
    and d4["boost"] == 1
    and sn4["today"]["pass"] == {"chat": 0, "direct": 2, "follow": 1}
    and sn4["today"]["skip"] == {"chat": 0}
    and sn4["today"]["admin"] == 0,
    "存档里的数被改坏了（跳过时间、次数、今天的数写成了字）：坏的扔掉，照样能掷、面板照样能看",
    err4 or (d4, sn4 and sn4["today"]),
)

print("\n[11 今天回了 / 跳过几次]")
b = Budget()
spend_to(b, 5, TUE_OFF)
b.decide("direct", "g", "1", now=TUE_OFF, roll=0.0)
b.decide("chat", "g", "1", now=TUE_OFF, roll=0.99)
b.decide("chat", "g", "2", admin=True, now=TUE_OFF)
t = b.today(TUE_OFF)
check(
    t["pass"] == {"direct": 1, "chat": 1}
    and t["skip"] == {"chat": 1}
    and t["admin"] == 1,
    "分场合记着",
    t,
)
check(b.today(TUE_OFF + 86400)["pass"] == {}, "第二天从 0 开始")
txt = Budget().status_text(TUE_OFF)
check(
    "回复意愿 100%" in txt
    and "火力全开：没开" in txt
    and "今天：回了 0 次 · 跳过 0 次" in txt
    and "最近 24 小时花了 $0（" in txt,
    "指令回的状态",
    txt,
)
b = Budget()
b.record(1000, 9000, 200, TUE_OFF)
b.reset_now("", TUE_OFF + 120)
txt = b.status_text(TUE_OFF + 130)
check(
    "重新算过" in txt
    and "2 次回话" not in txt
    and "1 次回话" in txt
    and "输出 $0.0001" in txt
    and "从 " in txt,
    "状态里有重新算、分输入 / 缓存 / 输出",
    txt,
)
check(
    budget.money(0) == "$0"
    and budget.money(0.00031) == "$0.0003"
    and budget.money(0.4567) == "$0.457"
    and budget.money(12.345) == "$12.35",
    "钱的说法：少的时候多留几位（不会显示成 $0.00）",
)

print("\n[12 配置说明（_conf_schema.json）跟代码对得上]")
schema = json.loads((d / "_conf_schema.json").read_text(encoding="utf-8"))["budget"][
    "items"
]
diff = {
    k: (v.get("default"), budget.DEFAULTS.get(k))
    for k, v in schema.items()
    if v.get("default") != budget.DEFAULTS.get(k)
}
check(
    not diff and set(schema) == set(budget.DEFAULTS),
    "每一项的默认值跟代码里一样、一项不多一项不少",
    (diff, set(schema) ^ set(budget.DEFAULTS)),
)
check(
    all(v.get("description") and v.get("hint") for v in schema.values()),
    "每一项都有中文说明和提示",
    [k for k, v in schema.items() if not (v.get("description") and v.get("hint"))],
)

print("\n[13 有人找它就一定回（v0.4.3，借作者自己的 QQ 机器人 11-07：不玩消失）]")
b = Budget()
check(b.cfg.get("never_skip_sought") is True, "默认开", b.cfg.get("never_skip_sought"))
spend_to(b, 1000, TUE_OFF)  # 花了一大堆：意愿掉到底线
dd = b.decide("direct", "g", "1", "小李", now=TUE_OFF, roll=0.999)
check(dd["ok"] and dd["by"] == "sought" and "一定回" in dd["why"], "群里 @ 它：花得再多也不掷、回", dd)
dp = b.decide("private", "p", "2", now=TUE_OFF, roll=0.999)
check(dp["ok"] and dp["by"] == "sought", "私聊：不掷、回", dp)
df = b.decide("follow", "g", "1", now=TUE_OFF, roll=0.999)
dc = b.decide("chat", "g", "1", now=TUE_OFF, roll=0.999)
dx = b.decide("extra", "g", "1", now=TUE_OFF, roll=0.999)
check(
    not df["ok"] and not dc["ok"] and not dx["ok"] and df["by"] == dc["by"] == dx["by"] == "roll",
    "接话、插话、冒泡照旧掷（省 token 省的是这些）",
    (df, dc, dx),
)
t13 = b.today(TUE_OFF)
check(t13["pass"].get("direct") == 1 and t13["pass"].get("private") == 1, "找它的算进「回了」", t13)
b.configure({"never_skip_sought": False})
d2 = b.decide("direct", "g", "1", now=TUE_OFF + 1, roll=0.999)
check(not d2["ok"] and d2["by"] == "roll", "关掉：群里找它又按次方掷（跟以前一样）", d2)
b.configure({"never_skip_sought": True})
b.set_full(1, "测试", TUE_OFF + 2)
check(b.decide("direct", "g", "1", now=TUE_OFF + 3)["by"] == "full", "火力全开时照旧记成「火力全开」")

ok = sum(1 for c, _ in RES if c)
bad = [m for c, m in RES if not c]
print(
    "\n"
    + "=" * 50
    + f"\n  结果：{ok} 项过了"
    + (f"，{len(bad)} 项没过" if bad else "，全过")
)
for m in bad:
    print("    -", m)
sys.exit(1 if bad else 0)
