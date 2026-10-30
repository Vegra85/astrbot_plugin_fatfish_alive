#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gatherer.py 单独测（不用装 AstrBot，几秒钟）：python tests/test_gatherer.py [插件目录 默认上一层]
时间都缩小了（基础 0.3 秒这种），真的在等，不是假时钟。"""

import asyncio
import importlib.util
import sys
import time
from pathlib import Path

d = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
spec = importlib.util.spec_from_file_location("gatherer", d / "core" / "gather.py")
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)
RES = []

# 缩小的时间：基础 0.3 / 只有图多等 0.3 / 很短多等 0.15 / 问句 0.15 / 最多 1.2 / 往回 1.0
FAST = {
    "base_sec": 0.3,
    "pic_extra_sec": 0.3,
    "short_extra_sec": 0.15,
    "quick_sec": 0.15,
    "max_sec": 1.2,
    "lookback_sec": 1.0,
    "settle_sec": 0.5,
}


def check(c, m, why=""):
    RES.append((bool(c), m))
    print(
        ("  [过]   " if c else "  [没过] ")
        + m
        + ("" if c or not why else f"\n           {str(why)[:600]}")
    )


def lines(mem):
    return [x.line for x in mem]


print("[1 等多久（看最后一条）]")
w = G.wait_for
check(w("我今天到家了啊", False) == 2.0, "普通的一句：基础 2 秒")
check(
    w("", True) == 4.0 and w("  ", True) == 4.0,
    "只有图 / 表情包、没字：多等 2 秒（4 秒）",
)
check(w("", False) == 2.0, "没字也没图（比如语音、文件）：基础 2 秒")
check(
    w("在吗", False) == 3.0 and w("那个", False) == 3.0 and w("嗯 嗯", False) == 3.0,
    "很短又没句末标点（「在吗」「那个」）：多等 1 秒（3 秒）",
)
check(
    w("好。", False) == 2.0 and w("好！", False) == 2.0, "很短但有句末标点：基础 2 秒"
)
check(
    w("这是啥？", False) == 1.2
    and w("你今天吃饭了吗", False) == 1.2
    and w("你在干嘛呢", False) == 1.2
    and w("what?", False) == 1.2,
    "问句（？?吗呢 结尾）：只等 1.2 秒",
)
check(
    w("我跟你说今天上班的时候碰到一件很离谱的事情", False) == 1.2,
    "12 个字以上的长句：只等 1.2 秒",
)
check(w("在吗？", False) == 1.2, "「在吗？」带了问号：按问句算（1.2 秒）")
check(
    w("在吗", False, dict(G.DEFAULTS, short_len=1)) == 1.2,
    "配置改了「几个字以内算很短」：跟着变",
)
check(
    w("你好", False, dict(G.DEFAULTS, base_sec=5.0)) == 6.0
    and w("", True, dict(G.DEFAULTS, pic_extra_sec=0)) == 2.0,
    "基础、多等几秒都能在配置里改",
)
check(
    w("这样啊", False, dict(G.DEFAULTS, quick_endings="啊")) == 3.0
    and w("就是这样啊", False, dict(G.DEFAULTS, quick_endings="啊")) == 1.2,
    "「以这些字结尾算问句」能改（短的还是先按「很短」算）",
)

print("\n[2 拼文字]")
check(
    G.merge_lines(["在吗", " 问你个事 ", "", "[图片]", "这是啥"])
    == "在吗\n问你个事\n[图片]\n这是啥",
    "按顺序换行拼起来，空的跳过、两头空格去掉",
)
check(G.merge_lines(["只有一条"]) == "只有一条", "只有一条不动")


async def carrier(g, key, line, pic=False, state="new"):
    m = g.note(key)
    g.fill(m, line, pic=pic, state=state)
    return m


async def scenario_private():
    print("\n[3 私聊连发三条：只回最后那条，带上前面的]")
    g = G.Gatherer(FAST)
    t0 = time.time()
    m1 = await carrier(g, "p|1", "我跟你说个事情吧")
    h1 = asyncio.ensure_future(g.hold(m1))
    await asyncio.sleep(0.1)
    m2 = await carrier(g, "p|1", "[图片]", pic=True)
    h2 = asyncio.ensure_future(g.hold(m2))
    await asyncio.sleep(0.1)
    m3 = await carrier(g, "p|1", "就是这个")
    h3 = asyncio.ensure_future(g.hold(m3))
    r1, r2, r3 = await h1, await h2, await h3
    took = time.time() - t0
    check(
        r1 == ("handoff", []) and r2 == ("handoff", []),
        "前两条让位（外壳会把它们静悄悄地停掉）",
        (r1, r2),
    )
    check(
        r3[0] == "go" and lines(r3[1]) == ["我跟你说个事情吧", "[图片]", "就是这个"],
        "最后一条带上三条（按顺序）",
        r3,
    )
    check(
        0.45 <= took <= 0.8,
        f"从最后一条起又等了一个基础时间（总共 {took:.2f} 秒）",
        took,
    )
    check(all(x.consumed for x in r3[1]), "这三条都记成「已经交给模型了」")
    r4 = await g.hold(m2)
    check(
        r4 == ("dup", []), "已经并进去的那条再来叫模型：dup（外壳停掉它，不回两遍）", r4
    )
    check(g.waiting() == [], "等完了：没有在等的")


async def scenario_group():
    print("\n[4 群里：@ 以后紧接着补的不带 @ 的也并进来；两个人互不影响]")
    g = G.Gatherer(FAST)
    a = await carrier(g, "g|7|A", "帮我看看这个")
    ha = asyncio.ensure_future(g.hold(a))
    b = await carrier(g, "g|7|B", "我也问一下")
    hb = asyncio.ensure_future(g.hold(b))
    await asyncio.sleep(0.15)
    q = g.note("g|7|A")
    g.fill(q, "[图片]", pic=True, state="quiet")  # A 没 @、补了一张图
    await asyncio.sleep(0.1)
    check(len(g.waiting()) == 2, "两个人各自在等", g.waiting())
    ra, rb = await ha, await hb
    check(
        ra[0] == "go" and lines(ra[1]) == ["帮我看看这个", "[图片]"],
        "A：@ 的那句 + 后来补的图",
        ra,
    )
    check(
        rb[0] == "go" and lines(rb[1]) == ["我也问一下"],
        "B：只有自己那句，不受 A 影响",
        rb,
    )

    print("\n[5 先发图、过几秒再 @：往回并（往回的秒数内）]")
    g = G.Gatherer(FAST)
    old = g.note("g|8|C")
    g.fill(old, "很早以前的", state="quiet")
    old.t -= 5  # 5 秒前的（往回只并 1 秒内的）
    p = g.note("g|8|C")
    g.fill(p, "[图片]", pic=True, state="quiet")
    await asyncio.sleep(0.2)
    c = await carrier(g, "g|8|C", "这是啥？")
    t0 = time.time()
    r = await g.hold(c)
    took = time.time() - t0
    check(
        r[0] == "go" and lines(r[1]) == ["[图片]", "这是啥？"],
        "1 秒内先发的图并进来了，5 秒前的没有",
        r,
    )
    check(took < 0.3, f"最后一条是问句：只等了 {took:.2f} 秒（问句只等 0.15）", took)
    r2 = await g.hold(await carrier(g, "g|8|C", "然后呢？"))
    check(
        r2[0] == "go" and lines(r2[1]) == ["然后呢？"],
        "下一轮：上一轮并过的不再并（很早以前那条太老了也不并）",
        r2,
    )


async def scenario_cap():
    print("\n[6 一直在发：从第一条算起最多等 max_sec]")
    g = G.Gatherer(FAST)
    t0 = time.time()
    res, tasks = [], []
    done_at = {}

    async def one(m):
        r = await g.hold(m)
        if r[0] == "go":
            done_at[m.id] = time.time() - t0
        return r

    for i in range(18):  # 每 0.1 秒一条，发 1.8 秒（上限 1.2）
        m = await carrier(g, "p|2", f"第{i}句话我接着说")
        tasks.append(asyncio.ensure_future(one(m)))
        await asyncio.sleep(0.1)
    res = [await t for t in tasks]
    gos = [r for r in res if r[0] == "go"]
    first = min(done_at.values()) if done_at else 99
    check(
        len(gos) == 2,
        f"一共回了 {len(gos)} 次（到上限回一次，后面的下一轮再回）",
        [lines(r[1]) for r in gos],
    )
    check(
        1.15 <= first <= 1.45,
        f"第一次在上限的时候回（{first:.2f} 秒，上限 1.2）",
        first,
    )
    allm = [x for r in gos for x in lines(r[1])]
    check(
        sorted(allm) == sorted(f"第{i}句话我接着说" for i in range(18))
        and len(allm) == 18,
        "18 条每条都回到了、没有一条回两遍",
        allm,
    )


async def scenario_settle():
    print("\n[7 到点了、他刚发的还没处理完（比如图还在下载）：等它一下]")
    g = G.Gatherer(FAST)
    m = await carrier(g, "p|3", "你看")
    h = asyncio.ensure_future(g.hold(m))
    await asyncio.sleep(0.2)
    slow = g.note("p|3")  # 来了一条，内容还不知道（early）
    await asyncio.sleep(0.5)  # 已经过了「你看」的点（0.45）
    check(not h.done(), "还没回：在等那条处理完")
    g.fill(slow, "[图片]", pic=True, state="quiet")
    r = await h
    check(r[0] == "go" and lines(r[1]) == ["你看", "[图片]"], "处理完了：一起回", r)

    g = G.Gatherer(FAST)
    m = await carrier(g, "p|4", "你看")
    h = asyncio.ensure_future(g.hold(m))
    await asyncio.sleep(0.05)
    slow = g.note("p|4")
    t0 = time.time()
    r = await h
    took = time.time() - t0
    check(
        r[0] == "go" and lines(r[1]) == ["你看"] and 0.7 <= took <= 1.1,
        f"一直处理不完：最多再等 settle_sec 就回（{took:.2f} 秒），没内容的那条不带",
        (r, took),
    )

    g = G.Gatherer(FAST)
    m = await carrier(g, "p|5", "你看")
    h = asyncio.ensure_future(g.hold(m))
    s = g.note("p|5")
    g.probe = lambda x: "dropped" if x is s else None  # 外壳问出来：它被别的插件停掉了
    t0 = time.time()
    r = await h
    check(
        r[0] == "go" and lines(r[1]) == ["你看"] and time.time() - t0 < 0.6,
        "问出来那条被别的插件停掉了：不等它、也不带它",
        r,
    )


async def scenario_misc():
    print("\n[8 内容已经知道、只是还没走到叫模型那一步的：直接算进来，不用等]")
    g = G.Gatherer(FAST)
    m = await carrier(g, "p|8", "你看")
    h = asyncio.ensure_future(g.hold(m))
    await asyncio.sleep(0.1)
    x = g.note("p|8")
    g.fill(x, "还有这个")  # new：内容知道了，会不会叫模型还不知道
    t0 = time.time()
    r = await h
    check(
        r[0] == "go" and lines(r[1]) == ["你看", "还有这个"] and time.time() - t0 < 0.5,
        "算进来、按它重新计时，到点就回（不多等）",
        (r, time.time() - t0),
    )
    check(await g.hold(x) == ("dup", []), "它后来再来叫模型：dup（不回两遍）")

    print("\n[9 旧消息会清掉（不说话的人也清）]")
    now = [1000.0]
    g = G.Gatherer(FAST, clock=lambda: now[0])
    g.note("old|1")
    now[0] += 500
    for _ in range(300):
        g.note("new|1")
    check(
        "old|1" not in g.piles and len(g.piles["new|1"].msgs) == 200,
        "很久没说话的人那一堆清掉了；一堆最多留 200 条",
        (list(g.piles), len(g.piles["new|1"].msgs)),
    )

    print("\n[10 被停掉的、指令不并；最多并几条]")
    g = G.Gatherer(dict(FAST, max_msgs=3))
    a = await carrier(g, "p|6", "一")
    b = g.note("p|6")
    g.fill(b, "拦住这句", state="dropped")
    c = g.note("p|6")
    g.fill(c, "/help")
    g.mark(c, "skip")
    for t in ("二", "三", "四"):
        x = g.note("p|6")
        g.fill(x, t, state="quiet")
    r = await g.hold(a)
    check(
        r[0] == "go" and lines(r[1]) == ["一", "三", "四"],
        "被停掉的、指令不并；超过 3 条只留最近的（要回的那条一定留着）",
        r,
    )
    check(
        all(x.consumed for x in g.piles["p|6"].msgs if x.line in ("二", "三", "四")),
        "丢掉的那几条也记成回过了（以后不会冒出来）",
    )
    g2 = G.Gatherer({"base_sec": "0.2", "max_sec": None, "nope": 1, "max_msgs": "x"})
    check(
        g2.cfg["base_sec"] == 0.2
        and g2.cfg["max_sec"] == 15.0
        and g2.cfg["max_msgs"] == 20
        and "nope" not in g2.cfg,
        "配置：字符串能转就转，空的 / 坏的 / 不认识的用默认",
        g2.cfg,
    )
    m = g.note("p|7")
    g.fill(m, "你好")
    g.mark(m, "carrier")
    check(m.state == "new", "外壳不能直接把消息标成 carrier（要走 hold）")
    r = await g.hold(m)
    check(r[0] == "go" and lines(r[1]) == ["你好"], "只有一条：照常回")


async def scenario_aside():
    print(
        "\n[11 在跟别人说的（@ 别人 / 引用别人，外壳标 aside）：照样重新计时，但不并进来]"
    )
    g = G.Gatherer(
        dict(FAST, base_sec=0.6, short_extra_sec=0.0, quick_sec=0.6)
    )  # 等多久都是 0.6 秒
    a = await carrier(g, "g|9|A", "帮我看看这个")
    h = asyncio.ensure_future(g.hold(a))
    await asyncio.sleep(0.4)
    x = g.note("g|9|A")
    g.fill(x, "@小李 等我一下")
    g.mark(x, "aside")
    await asyncio.sleep(
        0.4
    )  # 已经过了「帮我看看这个」自己的点（0.6），还没过 @ 别人那句的点（1.0）
    check(not h.done(), "@ 别人那句算他还在说：重新计时了，还在等")
    y = g.note("g|9|A")
    g.fill(y, "就是这张图", state="quiet")
    r = await h
    check(
        r[0] == "go" and lines(r[1]) == ["帮我看看这个", "就是这张图"],
        "@ 别人那句没并进来，他后来接着说的并进来了",
        r,
    )
    check(x.consumed, "@ 别人那句也算这一轮的（以后不会被往回并进来）")
    b = await carrier(g, "g|9|A", "还有一个问题？")
    r2 = await g.hold(b)
    check(
        r2[0] == "go" and lines(r2[1]) == ["还有一个问题？"],
        "下一轮往回并：上一轮的（包括 @ 别人那句）都不再并",
        r2,
    )
    z = g.note("g|9|A")
    g.fill(z, "@小李 你看")
    g.mark(z, "aside")
    c = await carrier(g, "g|9|A", "这个呢？")
    r3 = await g.hold(c)
    check(
        r3[0] == "go" and lines(r3[1]) == ["这个呢？"],
        "先 @ 别人、再 @ 机器人：往回并的时候也不带 @ 别人那句",
        r3,
    )


async def main():
    await scenario_private()
    await scenario_group()
    await scenario_cap()
    await scenario_settle()
    await scenario_misc()
    await scenario_aside()


asyncio.run(main())
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
