"""
大肥鱼 · 发消息（v0.2.0 新增）—— 判断逻辑（不依赖 AstrBot，单测能直接用）

三件事，都是「她把话发出去」那一步的：
  打字节奏（默认关）：真人打字有快有慢。每条按「字数 / 每秒几个字」等一会儿再发（每秒 2.5 个字，±20%）；
                    第一条先「读」完对方的话（每秒 15 个字，0.6~2 秒），已经想过的时间扣掉；每条 0.6~7 秒。
  打字刹车（默认关）：她分段发、还没发完，对方又说话了 → 后面没发的段落不发了，等下一轮一起回：
                    · 他把她正在回的那句撤回了 → 停
                    · 他又说了「算了 / 不用了 / 不是问这个…」→ 打到这条的 80% 以前都停
                    · 他又说了一句正经的（会再回他的、不是「嗯 / 哈哈 / 好的」这种）→ 只在这条刚开打（不到 30%）或者两条之间停
                    · 群里她打字的时候又来了好几句（默认 5 句）→ 两条之间停（话题可能已经过去了）
                    · 这条打到 80% 以后，先打完
  叫停（v0.4.0，默认开）：她分段发的时候，他把她在回的那句撤回了，或者明确说了「算了 / 不用了 / 别说了」这类
                    → 这段和后面没发的都不发了（不看打到哪了；第一段已经发出去了也一样）。
                    只是又补了一句（不是叫停）不算，那个归打字刹车（默认关）。
  群里引用：只引用「找她的那一条」（@ 她 / 叫她名字 / 回复她 / 接着跟她聊）——不是那个人后来随手补的一句；
                    好几个人在等她回 → 她这句里提到了谁就引用谁的（没提就引用这一轮在回的那个人），一定引用（分得清回谁）；
                    她自己插话（没人找她）→ 不引用；只有一个人、找她那条之后群里没别人说话（一来一回）→ 不引用；私聊不引用。

这里只放规则；外壳（modules/send_mod.py）负责在 AstrBot 真发每一段之前调用它们。
"""

import random
import re
import time
from collections import defaultdict, deque

DEFAULTS = {
    "enable": True,
    "typing_human": False,  # 打字节奏：每条按字数等一会儿再发（默认关：关着跟以前一样）
    "typing_cps": 2.5,  # 每秒打几个字（每条再随机 ±20%）
    "typing_max_sec": 7.0,  # 一条最多「打」几秒
    "typing_brake": False,  # 打字刹车：分段发的时候对方又说话了 → 后面没发的不发了（默认关）
    "brake_flood": 5,  # 群里她打字的时候又来了几句 就不往下发了
    "quote_mode": "smart",  # 群里引用哪条：smart = AstrBot 开了「引用回复」才管；always = 不管 AstrBot 开没开都由这里定；off = 不管
    "halt": True,  # （v0.4.0）叫停：他撤回了她在回的那句 / 明确说「算了 / 不用了 / 别说了」→ 剩下没发的段不发了
}

QUOTE_MODES = ("smart", "always", "off")

READ_CPS = 15.0  # 读对方的话：每秒几个字
READ_MIN, READ_MAX = 0.6, 2.0
SEG_MIN = 0.6  # 一条最少「打」几秒

# 对方说了这些 = 不用接着说了（打到这条的 80% 以前都停）
BRAKE_STRONG = re.compile(
    r"算了|不用了|不用回|别回了?|没事了|当我没说|不是问这个|不是这个意思|不是问你|说错了|发错了"
)
# 叫停（v0.4.0）：他明确不要她接着说了。只认开头就是这些的（前面可以有「那 / 哦 / 嗯」这种）：
# 「我算了一下」「不用了解」不算；「不是问这个 / 说错了」这种改口不算叫停（归打字刹车）
HALT = re.compile(
    r"^(?:(?:那|就|哦|噢|嗯|唉|哎|啊|呃|好吧|行吧|哎呀|你)[，,\s]*){0,3}"
    r"(?:算了(?![一下])|算啦|不用了(?!解)|不用回了?|不用说了|别回了?|别说了|别讲了|当我没说|没事了(?![吧吗？?])|不问了|发错了|打住|停停|闭嘴)"
)


def is_halt(text):
    """这句是不是明确叫停（「算了」「不用了」「别说了」…，开头就是）；[图片] 这种标记、开头的空白不算"""
    t = re.sub(r"\[[^\]]*\]", "", str(text or "")).strip()
    return bool(t) and bool(HALT.search(t))


# 这些只是应一声，不算「又说了一句」
BRAKE_ACK = re.compile(
    r"(嗯+|哦+|噢+|好+的?|好吧|行+|ok|收到|哈+|嘿+|嘻+|2333*|666*|6+|草|笑死|确实|是的|对+)[!！。.~～]*",
    re.IGNORECASE,
)


class Conf:
    """读配置：没填 / 填错了就用 DEFAULTS 里的（类型也照 DEFAULTS）"""

    def __init__(self, src=None):
        self.src = src if src is not None else {}

    def __call__(self, key):
        d = DEFAULTS[key]
        try:
            v = self.src.get(key, d)
        except (AttributeError, TypeError):
            v = d
        if v is None:
            return d
        if isinstance(d, bool):
            if isinstance(v, bool):
                return v
            return str(v).strip().lower() in ("1", "true", "yes", "on", "开")
        if isinstance(d, (int, float)):
            try:
                return type(d)(float(v))
            except (TypeError, ValueError):
                return d
        v = str(v).strip()
        if key == "quote_mode" and v not in QUOTE_MODES:
            return d
        return v


def chars(text):
    """算字数：空白和 [图片] [[标记]] 这种不算"""
    return len(re.sub(r"\[\[[^\]]*\]\]|\[[^\]]*\]|\s", "", text or ""))


def typing_delay(
    i, seg, lead=0.0, last_in="", cps=2.5, max_sec=7.0, uniform=random.uniform
):
    """第 i 条（从 0 数）要「打」几秒。
    lead：她想这句话已经花了几秒（第一条扣掉，别想完了再干等）；last_in：对方说的话（第一条先读完它）"""
    speed = max(0.8, float(cps) * uniform(0.8, 1.2))
    d = chars(seg) / speed
    if i == 0:
        read = min(READ_MAX, max(READ_MIN, chars(last_in) / READ_CPS))
        d += read - float(lead or 0)
    return min(float(max_sec), max(SEG_MIN, d))


class Ears:
    """她打字的时候，会话里又来了什么（打字刹车看这个）。都只在内存里
    key：会话（AstrBot 的 unified_msg_origin）"""

    def __init__(self, keep=50):
        self.last = {}  # (会话, 人) -> (时间, 说的话, 会不会再回他)
        self.woke = {}  # (会话, 人) -> 他最近一条会再回他的消息的时间
        self.count = defaultdict(int)  # 会话 -> 一共来了几句（不算她自己）
        self.recalled = defaultdict(lambda: deque(maxlen=keep))  # 会话 -> 撤回的消息 id
        self.stops = {}  # （v0.4.0）(会话, 人) -> (时间, 说的话)：他最近一次明确叫停（不是在跟别人说的）

    def heard(self, key, uid, text, woke=False, t=None, to_other=False):
        """有人说了一句（woke = 这句之后会再叫模型回他：私聊、@ 她、接她的话…；to_other = @ 别人 / 引用别人，在跟别人说）"""
        t = float(t or time.time())
        k = (str(key), str(uid))
        self.last[k] = (t, str(text or ""), bool(woke))
        if woke:
            self.woke[k] = t
        if not to_other and is_halt(text):
            self.stops[k] = (t, str(text or ""))
        self.count[str(key)] += 1
        if len(self.last) > 4000:  # 只涨不跌的表：清掉最旧的一半
            for x in sorted(self.last, key=lambda x: self.last[x][0])[:2000]:
                self.last.pop(x, None)
                self.woke.pop(x, None)
                self.stops.pop(x, None)

    def wake(self, key, uid, t):
        """这一句后来决定要回他了（群聊节奏判断完才知道）"""
        k = (str(key), str(uid))
        self.woke[k] = max(self.woke.get(k, 0.0), float(t))
        if k in self.last and self.last[k][0] == float(t):
            self.last[k] = (self.last[k][0], self.last[k][1], True)

    def recall(self, key, mid):
        if mid not in (None, ""):
            self.recalled[str(key)].append(str(mid))

    def heard_n(self, key):
        return self.count.get(str(key), 0)

    def halt_why(self, rnd):
        """叫停了吗（v0.4.0）→ ("recall", 原因) / ("word", 原因) / None。
        他把她在回的那句撤回了；或者她开始想以后，他明确说了「算了 / 不用了 / 别说了」（不是在跟别人说）。
        不看打到哪了：第一段已经发出去了也一样，剩下的都不发。只是又补了一句不算（那个归打字刹车）"""
        key, uid = str(rnd.get("key") or ""), str(rnd.get("uid") or "")
        mids = {str(x) for x in rnd.get("mids") or () if x not in (None, "")}
        if mids and mids & set(self.recalled.get(key, ())):
            return "recall", "他把她在回的那句撤回了"
        st = self.stops.get((key, uid))
        if uid and st and st[0] > float(rnd.get("t0") or 0):
            who = str(rnd.get("name") or "他")[:8]
            return "word", f"{who}说「{st[1][:20]}」"
        return None

    def why(self, rnd, strong=False, boundary=False):
        """要不要刹车 → 原因 / None
        rnd：这一轮 {"key", "uid", "name", "t0"（开始想的时间）, "mids"（她在回的消息 id）, "heard0", "group", "flood"}
        strong=True：这条已经打了一半（30%~80%），只认「算了」这种强信号；boundary=True：两条之间"""
        key, uid = str(rnd.get("key") or ""), str(rnd.get("uid") or "")
        mids = {str(x) for x in rnd.get("mids") or () if x not in (None, "")}
        if mids and mids & set(self.recalled.get(key, ())):
            return "他把她在回的那句撤回了"
        lt = self.last.get((key, uid))
        t0 = float(rnd.get("t0") or 0)
        if uid and lt and lt[0] > t0:
            txt = lt[1]
            who = str(rnd.get("name") or "他")[:8]
            if BRAKE_STRONG.search(txt):
                return f"{who}又说「{txt[:20]}」"
            core = re.sub(r"\[[^\]]*\]|\s", "", txt)
            queued = self.woke.get((key, uid), 0) > t0
            if (
                not strong
                and queued
                and len(core) > 2
                and not BRAKE_ACK.fullmatch(core)
            ):
                return f"{who}又说「{txt[:20]}」（下一轮一起回）"
        if boundary and rnd.get("group"):
            n = self.heard_n(key) - int(rnd.get("heard0") or 0)
            if n >= int(rnd.get("flood") or 5):
                return f"她打字的时候群里又来了 {n} 句（话题可能过去了）"
        return None


def progress_why(ears, rnd, prog):
    """打到这条的 prog（0~1）时要不要停：不到 30% 有值得停的就停；30%~80% 只认强信号；80% 以后先打完"""
    if prog >= 0.8:
        return None
    return ears.why(rnd, strong=prog >= 0.3)


def kept_text(full, sent):
    """刹车停了一半：原来整段话（full）里真发出去的那部分（sent：发出去的几段，按顺序）。
    在原文里按顺序找每一段，截到最后一段的末尾（保留原来的标点和换行）；找不到就把发出去的几段用换行拼起来"""
    full = str(full or "")
    pos = 0
    for seg in sent:
        seg = str(seg or "").strip()
        if not seg:
            continue
        j = full.find(seg, pos)
        if j < 0:
            return "\n".join(str(x).strip() for x in sent if str(x or "").strip())
        pos = j + len(seg)
    return full[:pos].rstrip()


# ═════════════════════ 群里引用哪条 ═════════════════════
def short(name):
    """群名片后面的数字 / 下划线去掉（跟群聊节奏里的一样）"""
    n = re.sub(r"[\d_\-\s]+$", "", name or "").strip()
    return n if len(n) >= 2 else (name or "")


def named_in(name, said):
    name = str(name or "").strip()
    if not name or not said:
        return False
    return any(len(x) >= 2 and x in said for x in {name, short(name)})


def pick_quote(group, head, waiting, said="", timeline=()):
    """这一句引用哪条 → 消息 id / None
    group：是不是群聊；head：这一轮在回的那个人 {"uid", "name", "mid"}（mid = 找她的那一条；她自己插话 → 没有 mid）；
    waiting：同一个群里别的、也在等她回的人 [{"uid", "name", "mid"}]；said：她这一轮要说的话；
    timeline：这个群最近的消息 [(时间, 谁, 消息 id)]，按先后（她自己说的 谁 = "self"），用来看「找她那条之后有没有别人说话」"""
    if not group:
        return None
    who, seen = [], set()
    for p in [head] + list(waiting or []):
        if not p or not p.get("mid") or str(p.get("uid")) in seen:
            continue
        seen.add(str(p.get("uid")))
        who.append(p)
    if not who:
        return None
    pick = None
    if len(who) > 1:
        named = [p for p in who if named_in(p.get("name"), said)]
        if len(named) == 1:
            pick = named[0]
    if pick is None:
        pick = next(
            (p for p in who if head and str(p.get("uid")) == str(head.get("uid"))),
            None,
        )
    if pick is None:
        return None
    mid = str(pick["mid"])
    tl = list(timeline or ())
    idx = next((i for i in range(len(tl) - 1, -1, -1) if str(tl[i][2]) == mid), None)
    if idx is None:
        return mid  # 找不到那条了（太早了）：引用上，免得认错
    between = [x for x in tl[idx + 1 :] if str(x[1]) != str(pick.get("uid"))]
    return mid if (between or len(who) > 1) else None


class Asks:
    """每个群里正在等她回的人（叫模型之前登记、她开始回这个人了就划掉；太久没划掉的自己过期）"""

    def __init__(self, ttl=180.0):
        self.ttl = float(ttl)
        self.g = defaultdict(dict)  # 会话 -> {事件 id: {"uid", "name", "mid", "t"}}

    def add(self, key, ev_id, uid, name, mid, t=None):
        if not mid:
            return
        self.g[str(key)][ev_id] = {
            "uid": str(uid),
            "name": str(name or ""),
            "mid": str(mid),
            "t": float(t or time.time()),
        }

    def done(self, key, ev_id):
        d = self.g.get(str(key))
        if d is not None:
            d.pop(ev_id, None)
            if not d:
                self.g.pop(str(key), None)

    def others(self, key, ev_id, uid, now=None):
        """同一个群里别的人（不是这个事件、不是这个人）还在等的：每人一条（最近那条）"""
        now = float(now or time.time())
        d = self.g.get(str(key)) or {}
        for k in [k for k, v in d.items() if now - v["t"] > self.ttl]:
            d.pop(k, None)
        out = {}
        for k, v in sorted(d.items(), key=lambda kv: kv[1]["t"]):
            if k == ev_id or v["uid"] == str(uid):
                continue
            out[v["uid"]] = v
        return list(out.values())


class Daily:
    KEYS = (
        "typed",
        "braked",
        "quote_kept",
        "quote_moved",
        "quote_dropped",
        "quote_added",
        "halted",  # （v0.4.0）叫停了几轮
        "halt_recall",  # 其中因为他撤回了
        "halt_word",  # 其中因为他说了「算了 / 不用了」
        "halt_segs",  # 一共没发几段
    )

    def __init__(self, d=None):
        self.d = dict(d) if isinstance(d, dict) else {}

    def today(self, day=None):
        day = day or time.strftime("%Y-%m-%d")
        if self.d.get("day") != day:
            self.d = {"day": day}
        for k in self.KEYS:
            if not isinstance(self.d.get(k), int):
                self.d[k] = 0
        return self.d

    def add(self, key, n=1, day=None):
        t = self.today(day)
        t[key] = int(t.get(key, 0)) + n
        return t[key]
