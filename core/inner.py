# -*- coding: utf-8 -*-
"""
鱼力max · 活人感 · 接话打分（v0.4.0 新增）—— 判断逻辑（不依赖 AstrBot，单测能直接用）

借的是 Inner Thoughts（Liu 等，CHI 2025，arXiv 2501.00383）：多人聊天里 AI 先在心里「想」，
再按 8 条内在动机给「要不要说出口」打分，过线才说；想说没说的，沉默越久越想说（每过一轮 × 1.02）。

这里只借打分这一步，而且全用规则算（不调模型、不花 token）：
  不先生成「想说的话」（那要多一次完整调用），只给「这时候开口」打分 ——
  群里一条没点名它的消息来了，看这条消息 + 前面的群聊 + 它自己刚说的话，8 条各打 0~5 分：
    相关     跟它有关吗：叫了它的名字、它刚回的就是他、它刚点了他的名、接了它的词；开头叫的是别人、跟别人的话更像、别人刚 @ 了他 → 低
    信息缺口 有没有在等答案：在问（冲它问的更高）、在求助；只是应一声、收尾句 → 0
    预期影响 开口有没有用：话越有内容越高，求助 / 问看法更高；只是应一声、只发了图 → 低
    紧迫     要不要现在说：紧跟着它说的、冲它问的、「急 / 在吗」→ 高
    连贯     接得上吗：它刚回的就是他、它说完以后他第一个说话；中间有别人插了好几句、他在跟别人一来一回、开头叫别人 → 低
    原创     能说点新的吗：在复读、只是应一声 → 低；别人已经在聊这个 → 低；有个问题刚抛出来 → 高一点
    平衡     轮得到它吗：它最近说得多 → 低；他很少说话 → 高（照顾少说话的人）；它已经不 @ 连着接了他好几轮 → 低
    节奏     插得进去吗：群里刷得快 → 低；在线聊着、冷了一阵有人开口 → 高一点；收尾句 → 0；它刚说完、他又不是在回它 → 低
  总分 = 8 条的平均 × 沉默加成（每过一句 × growth，默认 1.02；最多 × cap，默认 1.3）
         「过一句」= 它在这个群上次说话以后，群里别人又说了几句（它一说话就从头算）
  过线（默认 3.2）接；线上下 band（默认 0.4）以内算拿不准
理由：8 条里各自写一两个短的「为什么」，拼成一句给面板 / 日志看（高的两三条 + 拉低的一两条）。
"""

import re

from .rhythm import (
    CLOSING_WORDS,
    QUESTION,
    bigrams,
    calls_other,
    other_overlap,
    short_name,
)

KEYS = ("rel", "gap", "imp", "urg", "coh", "ori", "bal", "dyn")
NAMES = {
    "rel": "相关",
    "gap": "信息缺口",
    "imp": "预期影响",
    "urg": "紧迫",
    "coh": "连贯",
    "ori": "原创",
    "bal": "平衡",
    "dyn": "节奏",
}
MODES = ("shadow", "live", "off")
MODE_NAME = {"shadow": "影子", "live": "生效", "off": "关"}

# 求助 / 求推荐 / 问看法 / 着急：这几类是「开口有用」「要现在说」的信号
HELP = re.compile(
    r"有没有人|有人(?:知道|懂|会|用过|试过)|谁(?:知道|懂|会|有)|求助|求问|求推荐|求个|请问|帮我|帮忙|"
    r"怎么办|咋办|咋整|推荐(?:一下|个|下)?"
)
OPINION = re.compile(r"你觉得|大家觉得|怎么看|好不好|行不行|对吧|是吧|对不对|有道理吗")
URGENT = re.compile(r"急|赶紧|快点|马上|救命|在吗|在不在|快来|速来|有人吗")
# 只是应一声（跟打字刹车那个差不多，多了「谢谢」「啊这」这种；几个连着说也算：「好的谢谢」「哈哈哈 草」）
ACK = re.compile(
    r"(?:(?:嗯+|哦+|噢+|喔+|好+的?|好吧|行+|ok+|收到|哈+|嘿+|嘻+|呵+|h+|2333*|666*|6+|草|笑死|确实|是的|对+|"
    r"谢谢|多谢|感谢|谢了|thx|xswl|啊这|牛|强|绝了|可以|懂了|明白了?|知道了)[!！。.~～?？,，、]*)+",
    re.IGNORECASE,
)
# 收尾句：「好了 / 睡了 / 晚安」，几个连着说也算（「好了睡了」）
CLOSE_ANY = re.compile(r"^(?:(?:" + CLOSING_WORDS + r")[!！。.~～,，…啦了哦呀吧\s]*)+$")
# 在说它刚才的话（「你说的」「你刚才」）：在它说完的窗口里算跟它有关
REF_BOT = re.compile(r"你(?:说的|刚才|刚刚|上面|那句|之前说|讲的|的意思)")
# 对词（「接了它的词」「跟别人的话更像」）时不算的两字词：指人、指时间的虚词，不是话题（群聊节奏原来那套不动）
IT_STOP = set(
    "你说 说的 我说 他说 她说 你刚 刚才 刚刚 你讲 讲的 是哪 哪个 的是 是不 个呀 的那 那家 一下".split()
)


def _core(text):
    return re.sub(r"\[[^\]]*\]|\s", "", str(text or ""))


def is_closing(text):
    t = re.sub(r"\s+", "", str(text or ""))
    return bool(t) and len(t) <= 16 and bool(CLOSE_ANY.match(t))


def _clamp(v):
    return round(max(0.0, min(5.0, float(v))), 2)


def silence(history):
    """它在这个群上次说话以后，群里别人又说了几句（history 里没有它说的话 → 全算）"""
    n = 0
    for h in reversed(list(history or ())):
        if h.get("role") == "assistant":
            break
        if h.get("role") == "user":
            n += 1
    return n


def boost_of(quiet, growth=1.02, cap=1.3):
    """沉默加成：growth ^ 过了几句，最多 cap（growth ≤ 1 或 cap ≤ 1 → 不加）"""
    g, c = float(growth or 1.0), float(cap or 1.0)
    if g <= 1.0 or c <= 1.0:
        return 1.0
    return round(min(c, g ** max(0, int(quiet))), 3)


def _exchange(history, uid):
    """他是不是在跟某个人一来一回（最近 4 句别人的话只有他和另一个人、轮流说、最后一句是那个人）→ 那个人的名字 / ""。
    只看它上次说话以后的（它插在中间说过话就不算一来一回）"""
    tail = []
    for h in reversed(list(history or ())[-8:]):
        if h.get("role") == "assistant":
            break
        if h.get("role") == "user":
            tail.append(h)
        if len(tail) >= 4:
            break
    tail.reverse()
    us = [str(h.get("uid")) for h in tail]
    if len(us) < 2 or len(set(us)) != 2 or str(uid) not in us or us[-1] == str(uid):
        return ""
    if any(us[i] == us[i + 1] for i in range(len(us) - 1)):
        return ""
    return str(tail[-1].get("name") or "别人")


def evaluate(
    text,
    uid,
    name,
    now,
    history,
    bot=None,
    names=(),
    only_pic=False,
    active=False,
    noise=0.0,
    turns=0,
    window=150.0,
    growth=1.02,
    cap=1.3,
):
    """给「这时候开口」按 8 条打分。
    text / uid / name：这条消息和谁说的；history：这条之前的群聊（[{"role", "uid", "name", "text", "ts"}]，不含这一条）；
    bot：它刚说的那一轮 {"text", "sent"（说完的时间）, "t0"（开始想的时间）, "answered"（在回谁）}，没说过 → None / {}；
    names：它的名字和外号；active：在线聊着；noise：群里每分钟几句；turns：它已经不 @ 连着接了他几轮；window：接话的窗口（秒）
    → {"s": {条: 分}, "up" / "dn": {条: [往高 / 往低拉的短理由]}, "base": 平均, "quiet": 过了几句, "boost": 沉默加成,
       "total": 总分, "near": 在接话窗口里}"""
    text = str(text or "")
    uid = str(uid)
    core = _core(text)
    bot = bot or {}
    my = str(bot.get("text") or "")
    t_me = float(bot.get("sent") or 0)
    t0 = float(bot.get("t0") or t_me)
    answered = {str(u) for u in bot.get("answered") or ()}
    hist = [h for h in (history or []) if h.get("ts")]
    dt = now - t_me if t_me else None
    near = dt is not None and -5 <= dt <= float(window)
    names = [x for x in (names or ()) if x and len(x) >= 2]
    stop = set().union(*(bigrams(x) for x in names)) if names else set()
    stop |= IT_STOP
    named = bool(names) and any(x in text for x in names)
    after = (
        [h for h in hist if h.get("role") == "user" and float(h.get("ts") or 0) > t0]
        if t_me
        else []
    )
    others = [h for h in after if str(h.get("uid")) != uid]
    first = not any(str(h.get("uid")) == uid for h in after)
    common = bigrams(text, stop) & bigrams(my, stop) if (my and near) else set()
    hit, by = other_overlap(text, after if t_me else hist[-8:], uid, stop)
    callee = calls_other(text, hist, uid, names)
    q = bool(QUESTION.search(text))
    you = text.count("你") > text.count("你们")
    pair = _exchange(hist, uid)
    to_him = any(
        f"@{x}" in (h.get("text") or "")
        for h in others
        for x in {name, short_name(name)}
        if x
    )
    to_bot = named or (you and near and not callee and not to_him and not pair)
    ack = bool(core) and bool(ACK.fullmatch(core))
    closing = is_closing(text)
    helpq = bool(HELP.search(text))
    nick = short_name(name)
    nicked = near and len(nick) >= 2 and nick in my
    s = {}
    up, dn = (
        {k: [] for k in KEYS},
        {k: [] for k in KEYS},
    )  # 每条：往高拉的原因 / 往低拉的原因

    # 1 相关：跟它有关吗
    v = 2.0 if near else 1.0
    if named:
        v = 5.0
        up["rel"].append("叫了它的名字")
    if near and uid in answered:
        v += 1.5
        up["rel"].append("它刚回的就是他")
    if nicked:
        v += 1.5
        up["rel"].append("它刚点了他的名")
    if len(common) >= 2:
        v += 2.0
        up["rel"].append("接了它的词「" + "、".join(sorted(common)[:3]) + "」")
    elif common:
        v += 1.0
        up["rel"].append("接了它的词「" + "、".join(sorted(common)) + "」")
    if near and REF_BOT.search(text) and not callee:
        v += 1.5
        up["rel"].append("在说它刚才的话")
    elif you and near:
        v += 0.5
    if callee:
        v = min(v, 0.5)
        dn["rel"].append(f"开头叫的是「{callee[:8]}」")
    if len(hit) >= 2 and len(hit) > len(common):
        v -= 2.0
        dn["rel"].append(f"跟{(by or '别人')[:8]}的话更像")
    if to_him:
        v -= 1.5
        dn["rel"].append("别人刚 @ 了他")
    if only_pic:
        v -= 1.0
    s["rel"] = _clamp(v)

    # 2 信息缺口：有没有在等答案
    if closing:
        v = 0.0
        dn["gap"].append("收尾句")
    elif ack:
        v = 0.0
        dn["gap"].append("只是应一声")
    elif q:
        v = 4.5 if to_bot else 3.5
        up["gap"].append("冲它问的" if to_bot else "在问")
        if helpq:
            v += 0.5
            up["gap"].append("在求助")
    elif helpq:
        v = 3.0
        up["gap"].append("在求助")
    else:
        v = 1.0
    if callee or to_him:
        v = min(v, 1.0)  # 问的是别人
    if only_pic:
        v = min(v, 1.0)
    s["gap"] = _clamp(v)

    # 3 预期影响：开口有没有用
    n = len(core)
    v = 3.5 if n >= 15 else 2.5 if n >= 6 else 1.5 if n >= 3 else 0.5
    if q:
        v += 0.5
    if helpq:
        v += 1.0
        up["imp"].append("求助 / 求推荐")
    if OPINION.search(text):
        v += 0.5
        up["imp"].append("在问看法")
    if n >= 15 and not up["imp"]:
        up["imp"].append("话有内容")
    if callee or to_him or pair:
        v -= 1.0  # 在跟别人说：它开口用处不大
    if ack:
        v = min(v, 0.5)
    if closing:
        v = 0.0
    if only_pic:
        v = min(v, 1.0)
        dn["imp"].append("只发了图")
    s["imp"] = _clamp(v)

    # 4 紧迫：要不要现在说
    v = 1.0
    if near and dt <= 30:
        v += 2.0
        up["urg"].append(f"紧跟着它 {int(max(dt, 0))} 秒")
    elif near and dt <= 90:
        v += 1.0
    if q and to_bot:
        v += 1.5
    if URGENT.search(text):
        v += 1.5
        up["urg"].append("着急 / 在找人")
    if ack or closing:
        v = min(v, 1.0)
    s["urg"] = _clamp(v)

    # 5 连贯：接得上吗
    if not near:
        v = 1.0 + (1.0 if active else 0.0)
    else:
        v = 2.0
        if uid in answered:
            v += 1.5
        if first:
            v += 0.5
        if dt <= 30:
            v += 0.5
        if len(others) >= 2:
            v -= 1.5
            dn["coh"].append(f"中间有别人插了 {len(others)} 句")
    if callee:
        v -= 2.0
    if to_him:
        v -= 2.0
    if pair:
        v -= 1.5
        dn["coh"].append(f"他在跟{pair[:8]}一来一回")
    s["coh"] = _clamp(v)

    # 6 原创：能说点新的吗
    v = 3.0
    prev = [
        _core(h.get("text"))
        for h in hist
        if h.get("role") == "user" and str(h.get("uid")) != uid
    ][-3:]
    if core and core in prev:
        v = 0.5
        dn["ori"].append("在复读")
    if ack:
        v -= 1.5
    if len(hit) >= 2:
        v -= 1.0
        dn["ori"].append("别人已经在聊这个")
    if (q or helpq) and not ack:
        v += 0.5
    s["ori"] = _clamp(v)

    # 7 平衡：轮得到它吗（照顾少说话的人，它自己别说太多）
    recent = hist[-20:]
    total_n = len(recent)
    mine = sum(1 for h in recent if h.get("role") == "assistant")
    v = 3.0
    if total_n >= 5:
        share = mine / total_n
        if share >= 0.35:
            v = 1.0
            dn["bal"].append(f"它最近说得多（{total_n} 句里 {mine} 句）")
        elif share >= 0.2:
            v = 2.0
        elif mine == 0:
            v = 3.5
    his = sum(1 for h in recent if h.get("role") == "user" and str(h.get("uid")) == uid)
    if total_n >= 5 and his <= 1:
        v += 1.0
        up["bal"].append("他很少说话")
    elif total_n >= 10 and his / total_n >= 0.4:
        v -= 0.5
    if int(turns or 0) >= 3:
        v -= 1.0
        dn["bal"].append(f"已经连着接了他 {int(turns)} 轮")
    s["bal"] = _clamp(v)

    # 8 节奏：插得进去吗
    nz = float(noise or 0)
    v = 3.0
    if nz >= 8:
        v = 1.5
        dn["dyn"].append(f"群里刷得快（每分钟 {nz:g} 句）")
    elif nz >= 4:
        v = 2.5
    elif nz <= 1:
        v = 3.5
    if active:
        v += 0.5
    if hist and now - float(hist[-1].get("ts") or now) >= 120:
        v += 0.5
        up["dyn"].append("冷了一会儿有人开口")
    if near and dt < 10 and uid not in answered and not named:
        v -= 1.0
        dn["dyn"].append("它刚说完")
    if closing:
        v = 0.0
        dn["dyn"].append("收尾句")
    s["dyn"] = _clamp(v)

    base = round(sum(s[k] for k in KEYS) / len(KEYS), 3)
    quiet = silence(hist)
    boost = boost_of(quiet, growth, cap)
    return {
        "s": s,
        "up": up,
        "dn": dn,
        "base": base,
        "quiet": quiet,
        "boost": boost,
        "total": round(base * boost, 2),
        "near": near,
    }


def verdict(total, threshold=3.2, band=0.4):
    """总分 → take（过线够多）/ skip（差得多）/ maybe（线上下 band 以内，拿不准）"""
    t, b = float(threshold), max(0.0, float(band))
    hi, lo = round(t + b, 6), round(t - b, 6)  # 3.2 + 0.4 不能算成 3.6000000000000005
    if total >= hi:
        return "take"
    if total < lo:
        return "skip"
    return "maybe"


def kind_of(ev, answered=False):
    """要接的话算哪种：follow（在接它的话：在接话窗口里，而且跟它有关 / 接得上）/ chat（在线时挑着接）"""
    s = ev.get("s") or {}
    if ev.get("near") and (answered or s.get("rel", 0) >= 3 or s.get("coh", 0) >= 3.5):
        return "follow"
    return "chat"


def reason(ev, limit_hi=3, limit_lo=2):
    """一句理由：分高的几条往高拉的原因 + 分低的几条往低拉的原因（「；但」接上）；都没有就写「几条的名字 + 分」"""
    s, up, dn = ev.get("s") or {}, ev.get("up") or {}, ev.get("dn") or {}
    hi = [k for k in sorted(KEYS, key=lambda k: -s.get(k, 0)) if s.get(k, 0) >= 3.5]
    lo = [k for k in sorted(KEYS, key=lambda k: s.get(k, 0)) if s.get(k, 0) <= 1.5]
    a, b = [], []
    for k in hi:
        a += [w for w in up.get(k) or () if w not in a]
    for k in lo:
        b += [w for w in dn.get(k) or () if w not in b and w not in a]
    if not a:
        a = [f"{NAMES[k]} {s.get(k, 0):g}" for k in hi[:2]]
    if not b:
        b = [f"{NAMES[k]} {s.get(k, 0):g}" for k in lo[:2]]
    out = "、".join(a[:limit_hi]) or "没什么想接的理由"
    if b:
        out += "；但" + "、".join(b[:limit_lo])
    if ev.get("boost", 1.0) > 1.0:
        out += f"（它沉默了 {ev.get('quiet', 0)} 句 ×{ev['boost']:g}）"
    return out


# ═════════════════════ 今天的数 ═════════════════════
class Daily:
    """影子 / 生效时都记：打了几次分、打分说接 / 不接 / 拿不准、实际接了几次、两边不一样的、事后的证据"""

    KEYS = (
        "scored",  # 打了几次分
        "it_take",  # 打分说接
        "it_skip",  # 打分说不接
        "it_ask",  # 打分拿不准、又没问模型（影子里现在的规则没问模型的时候）
        "now_take",  # 实际接了（影子：现在的规则；生效：就是打分）
        "agree",  # 打分和实际一样（影子才有意义）
        "it_more",  # 打分说接、实际没接（影子：生效以后会多接的）
        "it_less",  # 实际接了、打分说不接（影子：生效以后会少接的）
        "at_after",  # 没接，他 90 秒内又 @ 了它（多半是漏接了）
        "at_after_it_skip",  # …其中打分也说不接（打分漏接的证据）
        "at_after_it_take",  # …其中打分说接（现在的规则漏了、打分能补上）
        "silent",  # 接了，模型自己选了不说（[[不说]]，多半是不该插的）
        "silent_it_take",  # …其中打分也说接（打分也会误插）
        "silent_it_skip",  # …其中打分说不接（打分能躲开的）
    )

    def __init__(self, d=None):
        self.d = dict(d) if isinstance(d, dict) else {}

    def today(self, day):
        if self.d.get("day") != day:
            self.d = {"day": day}
        for k in self.KEYS:
            if not isinstance(self.d.get(k), int):
                self.d[k] = 0
        return self.d

    def add(self, key, day, n=1):
        t = self.today(day)
        t[key] = int(t.get(key, 0)) + n
        return t[key]

    def note(self, rec, day):
        """一条打分记录定了（it / now 都有了）：记进今天的数"""
        self.add("scored", day)
        it, now = rec.get("it"), bool(rec.get("now"))
        self.add({"take": "it_take", "skip": "it_skip"}.get(it, "it_ask"), day)
        if now:
            self.add("now_take", day)
        if it in ("take", "skip"):
            if (it == "take") == now:
                self.add("agree", day)
            elif it == "take":
                self.add("it_more", day)
            else:
                self.add("it_less", day)

    def outcome(self, rec, what, day):
        """事后的证据：at_after（没接、他后来 @ 了它）/ silent（接了、模型选了不说）"""
        self.add(what, day)
        it = rec.get("it")
        if it in ("take", "skip"):
            self.add(f"{what}_it_{it}", day)
