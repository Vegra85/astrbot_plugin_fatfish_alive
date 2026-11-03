# -*- coding: utf-8 -*-
"""
攒一攒再回（核心逻辑，不依赖 AstrBot，NapCat 版也能直接用）

真人常常连着发好几条：「在吗」「问你个事」「[图]」「这是啥」。
这里不急着一条一条回：等他停下来，把这几条当一件事，只回一次。

几个词：
- 一个人一堆（pile）：私聊按人、群里按「群 + 人」。
- 要回的那条（carrier）：会去叫模型的那条（私聊每条都是；群里是 @ / 引用 / 唤醒词 / 别的插件决定要回的）。
- 等的时候同一个人又来一条要回的 → 前一条让位（外壳把它静悄悄地停掉），新的接着等、带上前面的。
- 又来一条不叫模型的（群里没 @ 的）→ 不让位，等完一起带上。
- 每来一条就重新计时（等多久看最后一条），从第一条要回的算起最多等 max_sec 秒。

外壳要做的事：
1. 一收到消息就 note()（越早越好，内容还不知道也行）；
2. 知道内容以后 fill()；知道它不会叫模型 → mark(m, "quiet")；不叫模型、而且是在跟别人说（@ 别人 / 引用别人）
   → mark(m, "aside")（算他还在说、照样重新计时，但不并进来）；是指令 / 不管的 → mark(m, "skip")；
   被别的插件停掉了 → mark(m, "dropped")；
3. 这条要叫模型了 → await hold(m)：
   - ("go", 这几条) → 把这几条并成一条交给模型；
   - ("handoff", []) → 后面又来了要回的，这条停掉（不调模型、什么都不发）；
   - ("dup", []) → 这条已经并进前一轮回过了，也停掉。
4. 可选：probe = 一个函数（这条消息后来怎么样了 → 新状态 / None），等的时候用来问还没定的那几条。
5. 可选（v0.3.0）：规则等完了再看一眼 ——
   typing = 一个函数（这条消息的人现在还在「正在输入」吗 → True / False）：还在输入就再等等（最多 typing_extra_sec 秒）；
   asker = 一个 async 函数（这条, 这几条 → 还要等几秒，0 = 说完了）：最后一句尾巴含糊（tail_vague）才问，一轮最多 tail_ask_max 次。
   没给 asker、或者 tail_ask 关着：尾巴含糊就固定多等 tail_extra_sec 秒。等的时候又来了新的 = 整个重新算。
6. （v0.4.0）他撤回了一条 → recall(key, 消息 id)：还排着、没交给模型的那条不再算进来（state = recalled）。
   在等的就是这条、也没有别的了 → hold() 给 ("recalled", [])，这一轮不回；还有别的（他前面发的几条）→ 只回剩下的。
   撤回通知比这条消息先到（内容还不知道）：外壳自己记着，等内容到了再 recall_msg(m)。
"""

import asyncio
import itertools
import re
import time

ENDS = "。！？!?…~～.)）」』>"  # 句末标点（有这些就不算「话没说完」）

DEFAULTS = {
    "base_sec": 2.0,  # 基础：等 2 秒
    "pic_extra_sec": 2.0,  # 最后一条只有图 / 表情包（没字）：多等 2 秒（后面多半还有一句）
    "short_extra_sec": 1.0,  # 最后一条很短又没有句末标点：多等 1 秒
    "short_len": 3,  # 多短算短（字数）
    "quick_sec": 1.2,  # 问句 / 长句：只等 1.2 秒
    "long_len": 12,  # 多长算长（字数）
    "quick_endings": "？?吗呢",  # 以这些结尾算问句
    "max_sec": 15.0,  # 从第一条要回的算起最多等多久（一直在发也得回）
    "lookback_sec": 20.0,  # 他先发了几条、过一会儿才 @：这么多秒内先发的也并进来
    "max_msgs": 20,  # 一次最多并几条
    "settle_sec": 3.0,  # 到点了、但后面还有他刚发的没处理完（比如图还在下载）：最多再等这么久
    "hold_phrases": True,  # （v0.1.0 活人感新增，借 jiehua 的思路）最后一条是「稍等 / 还没说完 / 等我发完」：先不回，多等一阵
    "hold_sec": 45.0,  # 说了「稍等」之后最多等多久（他后面接着说了就并进来一起回；一直没说才回）
    "tail_vague": True,  # （v0.3.0）最后一句尾巴含糊（那个 / 然后 / 还有、逗号、省略号、列举到一半）：规则等完再多看一眼
    "tail_ask": True,  # 尾巴含糊时问一次判断模型「他说完了吗」（关了：固定多等 tail_extra_sec 秒）
    "tail_extra_sec": 4.0,  # 不问模型时多等几秒
    "tail_ask_max": 2,  # 一轮最多问几次
    "typing_wait": True,  # （v0.3.0）私聊里他还在「正在输入」：多等一会儿（QQ 端要发「正在输入」通知，NapCat 会发）
    "typing_extra_sec": 8.0,  # 因为「正在输入」最多多等几秒
    "recall_drop": True,  # （v0.4.0）还排着没回、他把那条撤回了 → 那条不回（排着的只有那一条 → 这一轮不回）
}
TAIL_MAX_SEC = 12.0  # 问模型的回答最多等几秒；尾巴含糊多等的总上限也是这么多

UNDECIDED = ("early", "new")  # 还不知道会不会叫模型
# （v0.4.0）他撤回了的那条：state = "recalled"，不在 MEMBER 里 —— 不并进任何一轮，它自己来叫模型也不回
MEMBER = (
    "new",
    "quiet",
    "aside",
    "carrier",
)  # 算在这一轮里的（aside 只管计时，不并进来）


def clean_len(text):
    return len(re.sub(r"\s+", "", text or ""))


# 「稍等 / 还没说完 / 等我发完」：话没说完，别急着回（只看很短的话：长句里提到「等我」多半是别的意思）
HOLD = re.compile(
    r"稍等|稍候|^(?:等一下|等下|等等|等会儿?|再等等)[!！。.~～,，…]*$"
    r"|等我(?:一下|下|发完|说完|打完|写完|整理|组织|编辑|截个?图|找找)"
    r"|(?:还|我还|话还|事还)?没(?:说|发|打|写|讲)完|我先(?:说|发|打|写|讲)完|先别回|别急着回|先不要回|不要急着回"
)


def is_hold(text):
    t = re.sub(r"\s+", "", text or "")
    if not t or len(t) > 16 or t[-1] in "?？吗呢":
        return False
    return bool(HOLD.search(t))


# 尾巴含糊（v0.3.0，借作者自己的 QQ 机器人 v1.16.3）：结尾是「那个 / 然后 / 还有 / 就是 / 而且 / 但是 / 所以 / 比如 / 首先 / 第一」、
# 逗号 顿号 冒号 省略号，或者正在列举（有「1.」没有「2.」）→ 多半话没说完
TAIL_WORD = re.compile(
    r"(?:那个|然后|还有|就是|而且|但是|所以|比如|首先|第一)[\s，,、…~～.。]*$"
)
TAIL_PUNCT = re.compile(r"(?:[，,、：:…]|\.{2,}|。{2,})\s*$")


def tail_vague(text):
    """最后一句的尾巴含糊吗（[图片] 这种标记不算字）"""
    t = re.sub(r"\[[^\]]*\]", "", str(text or "")).strip()
    if not t:
        return False
    if TAIL_WORD.search(t) or TAIL_PUNCT.search(t):
        return True
    one = re.search(r"(?:^|[\s：:，,])(?:1\s*[.、)）](?!\d)|①|第一)", t)
    two = re.search(r"(?:^|[\s：:，,；;。])(?:2\s*[.、)）](?!\d)|②|第二)", t)
    return bool(one and not two)


def wait_for(text, pic, cfg=None):
    """最后一条是这样的话，再等几秒。按顺序看，先对上哪条算哪条：
    没字只有图 → 基础 + 多等；很短又没句末标点 → 基础 + 多等一点；问句 / 长句 → 只等一小会儿；都不是 → 基础"""
    c = cfg or DEFAULTS
    base = float(c.get("base_sec", 2.0))
    t = re.sub(r"\s+", "", text or "")
    if not t:
        return base + (float(c.get("pic_extra_sec", 2.0)) if pic else 0.0)
    if c.get("hold_phrases", True) and is_hold(t):
        return max(base, float(c.get("hold_sec", 45.0)))
    if len(t) <= int(c.get("short_len", 3)) and t[-1] not in ENDS:
        return base + float(c.get("short_extra_sec", 1.0))
    if t[-1] in str(c.get("quick_endings", "？?吗呢")) or len(t) >= int(
        c.get("long_len", 12)
    ):
        return float(c.get("quick_sec", 1.2))
    return base


def merge_lines(lines):
    """几条消息的文字按顺序用换行拼起来（空的跳过）"""
    return "\n".join(x.strip() for x in lines if x and x.strip())


class Msg:
    """一条消息。line：拿来拼的那行字（没字的图就是「[图片]」）；pic：只有图 / 表情、没字；data：外壳自己的东西"""

    __slots__ = (
        "id",
        "key",
        "t",
        "line",
        "pic",
        "state",
        "ev",
        "data",
        "consumed",
        "superseded",
        "first",
        "start",
        "finished",
        "who",
        "where",
        "more",
    )

    def __init__(self, mid, key, t, ev=None):
        self.id, self.key, self.t, self.ev = mid, key, t, ev
        self.line, self.pic, self.data = "", False, None
        self.state = "early"
        self.consumed = False  # 已经并进某一轮交给模型了
        self.superseded = None  # 让位给了哪一条
        self.first = t  # 这一串里第一条要回的什么时候来的（算 15 秒上限）
        self.start = t  # 从什么时候起的消息算进来
        self.finished = False  # 等完了
        self.who, self.where = "", ""
        self.more = {}  # （v0.3.0）规则等完以后又多等了什么：typed 输入多等的秒数 / asked 问了几次 / tail_waits 尾巴含糊多等了几次

    def __repr__(self):
        return f"<Msg {self.id} {self.state} {self.line[:12]!r}>"


class Pile:
    def __init__(self):
        self.msgs = []  # 按收到的顺序
        self.carrier = None  # 正在等的那条
        self.changed = asyncio.Event()

    def kick(self):
        self.changed.set()


class Gatherer:
    def __init__(self, cfg=None, clock=time.time):
        self.cfg = dict(DEFAULTS)
        self.set_cfg(cfg or {})
        self.clock = clock
        self.piles = {}
        self.probe = None
        self.typing = None  # （v0.3.0）这条消息的人还在「正在输入」吗
        self.asker = None  # （v0.3.0）尾巴含糊时问「还要等几秒」的 async 函数
        self._ids = itertools.count(1)

    def set_cfg(self, cfg):
        for k, v in (cfg or {}).items():
            if k in DEFAULTS and v is not None:
                if isinstance(DEFAULTS[k], bool) and not isinstance(v, bool):
                    v = str(v).strip().lower() in ("1", "true", "yes", "on", "开")
                try:
                    self.cfg[k] = type(DEFAULTS[k])(v)
                except (TypeError, ValueError):
                    pass

    # ───────── 外壳告诉这里：来了一条 / 内容是什么 / 后来怎么样了 ─────────
    def note(self, key, t=None, ev=None):
        now = self.clock()
        pile = self.piles.get(key)
        if pile is None:
            pile = self.piles[key] = Pile()
        m = Msg(next(self._ids), key, float(t if t is not None else now), ev)
        pile.msgs.append(m)
        self._prune(pile, now)
        if m.id % 256 == 0:  # 隔一阵把所有人的旧消息都清一遍（不说话的人也清）
            for p in list(self.piles.values()):
                self._prune(p, now)
        pile.kick()
        return m

    def fill(self, m, line, pic=False, data=None, state="new"):
        m.line, m.pic, m.data = line or "", bool(pic), data
        if m.state in UNDECIDED:
            m.state = state
        self._kick(m)

    def mark(self, m, state):
        """quiet：不会叫模型（还会并进别人那一轮）；aside：不会叫模型、在跟别人说（重新计时，但不并进来）；
        skip：指令 / 不管的；dropped：被别的插件停掉了"""
        if (
            m is None
            or state not in ("quiet", "aside", "skip", "dropped")
            or (m.state == "carrier" and state != "dropped")
        ):
            return
        if m.state in UNDECIDED or state == "dropped":
            m.state = state
        self._kick(m)

    def _kick(self, m):
        pile = self.piles.get(m.key)
        if pile is not None:
            pile.kick()

    # ───────── 他撤回了一条（v0.4.0）─────────
    def recall(self, key, mid):
        """他撤回了消息 id 是 mid 的那条：还排着、没交给模型的 → 不再算进来。→ 那条 Msg / None（不在这里、已经交给模型了、开关关着）"""
        if not self.cfg.get("recall_drop", True) or mid in (None, ""):
            return None
        pile = self.piles.get(key)
        if pile is None:
            return None
        for x in pile.msgs:
            if str((x.data or {}).get("mid") or "") == str(mid):
                return x if self.recall_msg(x) else None
        return None

    def recall_msg(self, m):
        """这一条撤回了（撤回通知比它先到的那种，外壳拿到内容以后调）→ 记上了没有"""
        if m is None or m.consumed or not self.cfg.get("recall_drop", True):
            return False
        if m.state != "recalled":
            m.state = "recalled"
            self._kick(m)
        return True

    def _prune(self, pile, now):
        keep = max(self.cfg["lookback_sec"], self.cfg["max_sec"]) + 60
        live = pile.carrier
        pile.msgs = [x for x in pile.msgs if now - x.t < keep or x is live][-200:]
        for k in [k for k, p in self.piles.items() if not p.msgs and p.carrier is None]:
            self.piles.pop(k, None)

    def waiting(self):
        """现在正在等的：[(key, 已经攒了几条, 从第一条起等了几秒)]"""
        now, out = self.clock(), []
        for k, p in self.piles.items():
            m = p.carrier
            if m is not None and not m.finished:
                n = len(self._members(p, m))
                out.append((k, n, round(now - m.first, 1)))
        return out

    # ───────── 要回了：等一等 ─────────
    def _members(self, pile, m):
        return [
            x
            for x in pile.msgs
            if not x.consumed and x.state in MEMBER and x.t >= m.start - 0.001
        ]

    def _probe(self, pile):
        if self.probe is None:
            return
        for x in pile.msgs:
            if x.state in UNDECIDED and not x.consumed:
                try:
                    st = self.probe(x)
                except Exception:
                    st = None
                if st:
                    x.state = st

    async def _nap(self, pile, sec):
        pile.changed.clear()
        try:
            await asyncio.wait_for(pile.changed.wait(), max(0.01, sec))
        except asyncio.TimeoutError:
            pass

    def deadline(self, pile, m):
        mem = self._members(pile, m)
        last = mem[-1] if mem else m
        wait = wait_for(
            "" if last.pic else last.line, last.pic, self.cfg
        )  # pic = 只有图、没字
        cap = self.cfg["max_sec"]
        if not last.pic and self.cfg.get("hold_phrases", True) and is_hold(last.line):
            cap += self.cfg.get(
                "hold_sec", 45.0
            )  # 说了「稍等」：上限也跟着放宽（他一直在发的话还是照常封顶）
        return min(last.t + wait, m.first + cap)

    def _carriers(self, pile, m):
        """（v0.4.0）这一轮里别的「要回的」（他前面发的、让过位的几条）"""
        return [
            x for x in self._members(pile, m) if x.state == "carrier" and x is not m
        ]

    def _sig(self, pile, m):
        return tuple(x.id for x in self._members(pile, m))

    async def _more(self, pile, m, st):
        """规则等完了：还要不要再等（v0.3.0）→ 再等几秒（0 = 不等了）
        ① 他还在「正在输入」→ 一次 1 秒，最多 typing_extra_sec ② 最后一句尾巴含糊 → 问 asker / 固定多等"""
        c = self.cfg
        now = self.clock()
        mem = self._members(pile, m)
        last = mem[-1] if mem else m
        if c.get("typing_wait", True) and self.typing is not None:
            room = float(c.get("typing_extra_sec", 8.0)) - st.get("typed", 0.0)
            try:
                typing = room > 0 and bool(self.typing(m))
            except Exception:
                typing = False
            if typing:
                step = min(1.0, room)
                st["typed"] = st.get("typed", 0.0) + step
                st["why"] = "typing"
                return step
        sig = self._sig(pile, m)
        if (
            not c.get("tail_vague", True)
            or last.pic
            or st.get("tail_sig") == sig  # 这一串已经看过一眼了（多等完没来新的 → 回）
            or st.get("asked", 0) >= int(c.get("tail_ask_max", 2))
            or not tail_vague(last.line)
            or (c.get("hold_phrases", True) and is_hold(last.line))
        ):
            return 0.0
        st["tail_sig"] = sig
        st["asked"] = st.get("asked", 0) + 1
        if c.get("tail_ask", True) and self.asker is not None:
            try:
                n = float(await self.asker(m, mem) or 0)
            except Exception:
                n = 0.0
            now = self.clock()
        else:
            n = float(c.get("tail_extra_sec", 4.0))
        room = m.first + float(c["max_sec"]) + TAIL_MAX_SEC - now
        n = max(0.0, min(n, TAIL_MAX_SEC, room))
        if n > 0:
            st["why"] = "tail"
            st["tail_waits"] = st.get("tail_waits", 0) + 1
        return n

    async def hold(self, m):
        """→ (结果, 这几条)：结果是 go / handoff / dup / recalled（v0.4.0：在等的这条他撤回了、也没有别的要回 → 这一轮不回）"""
        pile = self.piles.get(m.key)
        if pile is None or m not in pile.msgs:  # 太久以前的（已经清掉了）：不攒
            return "go", [m]
        if m.consumed:
            return "dup", []
        if m.state == "recalled":  # （v0.4.0）还没轮到它叫模型，他就撤回了
            m.consumed = True
            return "recalled", []
        c = self.cfg
        m.state = "carrier"
        prev = pile.carrier
        if prev is not None and prev is not m and not prev.finished:
            prev.superseded = m  # 前一条让位，这条接着等、带上前面的
            m.first, m.start = min(prev.first, m.t), min(prev.start, m.t)
        else:
            m.first, m.start = m.t, m.t - c["lookback_sec"]
        pile.carrier = m
        pile.kick()
        settle_end = None
        more = {}  # 规则等完以后又多等的（v0.3.0）：{"until", "sig", "typed", "asked", ...}
        try:
            while True:
                if m.superseded is not None:
                    return "handoff", []
                if m.state == "recalled" and not self._carriers(pile, m):
                    break  # （v0.4.0）在等的这条他撤回了、也没有别的要回的了：不用再等
                self._probe(pile)
                now = self.clock()
                dl = self.deadline(pile, m)
                if more.get("until") and more.get("sig") == self._sig(pile, m):
                    dl = max(dl, more["until"])  # 还是那几条：照多等的那个点
                if now >= dl:
                    # 到点了：他还有刚发的、内容还不知道（比如图还在下载）→ 再等一下它。
                    # 内容知道了的（new）不用等：已经算在里面了，它以后再来叫模型就是 dup（不回两遍）
                    later = [
                        x
                        for x in pile.msgs
                        if x is not m
                        and x.state == "early"
                        and not x.consumed
                        and x.t >= m.start
                        and (x.id > m.id or now - x.t < c["settle_sec"] + 5)
                    ]
                    if later:
                        if settle_end is None:
                            settle_end = now + c["settle_sec"]
                        if now < settle_end:
                            await self._nap(pile, 0.1)
                            continue
                    sig = self._sig(pile, m)
                    n = await self._more(pile, m, more)
                    if m.superseded is not None:
                        return "handoff", []
                    if n > 0:
                        more["until"], more["sig"] = self.clock() + n, sig
                        continue
                    break
                await self._nap(pile, dl - now)
            mem = self._members(pile, m)
            gone = m.state == "recalled"  # （v0.4.0）在等的这条他撤回了：只回剩下的
            if gone and not self._carriers(pile, m):
                # 剩下的都不是要回的（群里没 @ 的随口话 / 还没定的）：这一轮不回；它们不标成并过，自己要回的话会自己来
                m.consumed = True
                return "recalled", []
            if m not in mem and not gone:
                mem.append(m)
            mem.sort(key=lambda x: x.id)
            m.consumed = True
            for x in mem:
                x.consumed = True
            mem = [
                x for x in mem if x.state != "aside"
            ]  # 在跟别人说的：算过时间了，不并进来（也不会再被往回并）
            if (
                not mem
            ):  # （v0.4.0）撤回的就是这条，剩下的都是在跟别人说的 / 没有：这一轮不回
                return "recalled", []
            mx = max(1, int(c["max_msgs"]))
            if len(mem) > mx:  # 太多了：留最近的几条（这条自己一定留着）
                if gone:
                    mem = mem[-mx:]
                else:
                    rest = [x for x in mem if x is not m][-(mx - 1) :] if mx > 1 else []
                    mem = sorted(rest + [m], key=lambda x: x.id)
            m.more = {
                k: v for k, v in more.items() if k in ("typed", "asked", "tail_waits")
            }
            return "go", mem
        finally:
            m.finished = True
            if pile.carrier is m:
                pile.carrier = None
            pile.kick()
