# -*- coding: utf-8 -*-
"""
鱼力max · 活人感 · 群聊节奏 —— 判断逻辑（不依赖 AstrBot：单测、别的框架都能直接用）

接话（Followup）：机器人在群里说完话的一小会儿里（默认 150 秒），有人发了一条没 @ 它、没 @ 别人、没引用别人的消息：
  先看几个信号打分 —— 紧跟着它说的、用「你」、在提问、跟它刚说的话有重合的词、是它刚回过的人、它刚点名的人、叫了它的名字；
                      中间又有别人插进来、别人刚 @ 了这个人、光发张图 就扣分
  分高 → 当成在跟它说（不用 @ 也接）；分低 → 不是；拿不准 → 让模型看一眼最近几条判断
  认错对象（v0.2.0）：这句跟群里别人刚说的话更像 / 开头叫的是群里别人的名字 → 扣分（多半在跟那个人说）；
                      「好几遍 / 这几天 / 几乎」里的「几」不算在问
  防止跟另一个机器人对聊停不下来：同一个人不 @ 它、它连着接最多几轮；有人 @ 它 / 10 分钟没接 就重新算

在线一阵（Rhythm）：观望 → 在线（跟着聊一阵）→ 收尾潜水
  观望：平时的样子，被 @ 才回
  在线：它在群里说上话以后「在线」一阵子（默认 8~15 分钟）；这期间没点名的话也挑着接（有概率、两句之间有间隔、每轮有上限）
        别人在 @ 别人 / 引用别人 = 不是跟它说的，不抢话
  冷场：在线时大家突然都不说话了 → 有一定概率顺着话题试探一句；还是没人理 → 安静回观望
  到点：大家还在聊 → 有一定概率说句收尾话再回观望；收尾后一阵子不主动插话（被 @ 照样回）
  状态只在内存里，重启就回到观望
"""

import random
import re
import time

SILENT = "[[不说]]"

DEFAULTS = {
    "enable": True,
    # ── 接话 ──
    "follow_enable": True,
    "window_sec": 150,  # 它说完话之后多久之内的消息 才考虑是不是在接它的话
    "yes_score": 4.0,  # 分数到这个 直接当成在跟它说
    "no_score": 1.5,  # 分数不到这个 直接当成不是
    "ask_model": True,  # 中间的让模型判断
    "judge_provider_id": "",  # 判断用哪个模型（空 = 这个群在用的对话模型）
    "judge_timeout_sec": 8.0,  # 模型多久没答 算「不是」
    "judge_daily_max": 300,  # 每天最多让模型判断几次
    "max_turns": 5,  # 同一个人不 @ 它 最多连着接几轮（防两个机器人对聊停不下来）
    "owner_max_turns": 10,  # AstrBot 管理员多一点
    "turn_reset_min": 10.0,  # 多久没接 轮数清零
    "pic_prob": 0.4,  # 光发图/表情 就算在接它 也只按这个概率回
    # ── 在线一阵 ──
    "rhythm_enable": True,
    "online_min": 8.0,  # 每次在线多久（分钟，在这两个数之间随机）
    "online_max": 15.0,
    "max_minutes": 40.0,  # 被一直 @ 着也最多在线这么久
    "reply_prob": 0.3,  # 在线时 没点名的消息 接话的概率
    "pic_factor": 0.3,  # 光发图/表情 概率再打个折
    "min_gap_sec": 45,  # 在线时挑着接 两句之间至少隔多久
    "max_replies": 6,  # 每次在线 挑着接最多几次
    # ── 冷场试探 / 收尾 ──
    "idle_min": 5.0,  # 在线时多久没人说话算冷场
    "probe_prob": 0.3,  # 冷场时试探一句的概率
    "probe_wait_min": 2.0,  # 试探后等多久没人理就回观望
    "exit_line": True,  # 到点时说句收尾话
    "exit_line_prob": 0.7,
    "exit_if_chat_within_min": 3.0,  # 到点时 最近几分钟还有人在聊 才说收尾话（没人聊就默默走）
    "cooldown_min": 10.0,  # 收尾后多久内不主动插话（被 @ 照样回）
    "line_provider_id": "",  # 试探 / 收尾用哪个模型（空 = 这个群在用的对话模型）
    "lines_per_hour": 4,  # 每个群每小时最多主动说几句（试探 + 收尾）
    # ── 其他 ──
    "bot_names": [],  # 机器人的名字和外号（空 = 用它的 QQ 昵称 / 群名片）
    "skip_ai": True,  # 认 AI 插件认出来的别人家的 AI：不接话、不挑着接
    "groups": [],  # 只在这些群里用（空 = 所有群）
    # ── （v0.1.0 活人感新增）──
    "closing_skip": True,  # 「好了 / 就这样 / 睡了」这类收尾句：不接话、不挑着接（借 jiehua 的思路；被 @ 照样回）
    "topic_overlap": True,  # 接话：不是它刚回的那个人、又没叫它名字、话题词没对上两个 → 扣分（借 natural_followup 的思路）
    "topic_penalty": 1.0,  # 话题词没对上两个扣几分
    # ── （v0.2.0 新增：接话认错对象）──
    "other_overlap": True,  # 这句跟群里别人刚说的话对上两个以上的词、又比跟它的话更像 → 扣分（多半在回那个人）
    "other_penalty": 2.5,  # 上面这条扣几分
    "call_other": True,  # 开头叫的是群里别人的名字（「小李来段语音」）→ 扣分（多半在跟那个人说）
    "call_other_penalty": 4.0,  # 上面这条扣几分
    # ── （v0.4.0 新增：接话打分，借 Inner Thoughts；判断在 core/inner.py）──
    "inner_mode": "shadow",  # shadow = 只算分、记理由、面板能看，不改接不接；live = 按分数决定接不接；off = 不算
    "inner_threshold": 3.2,  # 过几分接（每条 0~5 分，取平均再乘沉默加成）
    "inner_band": 0.4,  # 线上下几分以内算拿不准（接话那种问一下判断模型；挑着接那种按线定）
    "inner_growth": 1.02,  # 沉默加成：它没说话的时候群里每过一句 × 几
    "inner_growth_cap": 1.3,  # 沉默加成最多 × 几
    "inner_log_days": 14,  # 打分记录（plugin_data 里每天一个文件）留几天；0 = 不写文件
}
INNER_MODES = ("shadow", "live", "off")

_CJK = re.compile(r"[㐀-鿿]")
# 这些两字词太常见 不算「接了它的词」
_STOP = set(
    "什么 怎么 这个 那个 不是 没有 就是 你们 我们 他们 一下 一个 还是 可以 知道 觉得 现在 真的 这么 那么 "
    "然后 但是 因为 所以 已经 还有 这样 那样 有点 一点 哈哈 呵呵 嘿嘿 啊啊 嗯嗯 哦哦 "
    "好的 是的 对的 还好 好吧 行吧 啊这 咱们 一样".split()
)
# 「几」只在真的问数量时算问：「好几遍 / 这几天 / 几乎 / 几次」不是在问（v0.2.0：「你已经说过好几遍了」被当成在问它）
QUESTION = re.compile(
    r"[?？]|吗|呢|啥|什么|怎么|怎样|如何|为什么|为啥|咋|哪|(?<![好这那前后头末])几(?![乎遍次回])|多少"
    r"|是不是|有没有|行不行|好不好|能不能|要不要"
)


class Conf:
    """读配置：没填 / 填错了就用 DEFAULTS 里的（类型也照 DEFAULTS）"""

    def __init__(self, src=None):
        self.src = src if src is not None else {}

    def __call__(self, key):
        d = DEFAULTS[key]
        try:
            v = self.src.get(key, d)
        except Exception:
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
        if isinstance(d, list):
            if isinstance(v, str):
                v = re.split(r"[,，\s]+", v)
            return [str(x).strip() for x in (v or []) if str(x).strip()]
        v = str(v)
        if key == "inner_mode" and v.strip() not in INNER_MODES:
            return d  # 填错了：按默认（影子）
        return v.strip() if key == "inner_mode" else v


def bigrams(s, stop=()):
    s = re.sub(r"\s+", "", (s or "").lower())
    return (
        {
            s[i : i + 2]
            for i in range(len(s) - 1)
            if _CJK.match(s[i]) and _CJK.match(s[i + 1])
        }
        - _STOP
        - set(stop)
    )


# 收尾句：说完就走的话（不是在等回答）
CLOSING_WORDS = (
    r"好了|好啦|好嘞|就这样|就这样吧|先这样|先这样吧|就先这样|睡了|我睡了|去睡了|睡觉了|睡啦|晚安|拜拜|拜了|再见|88|886"
    r"|不聊了|先下了|下了|走了|溜了|撤了|散了|去忙了|先忙了|我去忙了"
)  # （v0.4.0 拆出来：接话打分里「好了睡了」这种连着说的也认）
CLOSING = re.compile(r"^(?:" + CLOSING_WORDS + r")[!！。.~～,，…啦了哦呀吧]*$")


def is_closing(text):
    t = re.sub(r"\s+", "", text or "")
    return bool(t) and len(t) <= 10 and bool(CLOSING.match(t))


def short_name(name):
    """群名片后面的数字 / 下划线去掉（「老王123」→「老王」），太短就用原来的"""
    n = re.sub(r"[\d_\-\s]+$", "", name or "").strip()
    return n if len(n) >= 2 else (name or "")


def ago(sec):
    sec = max(0, int(sec))
    return f"{sec}秒前" if sec < 60 else f"{sec // 60}分钟前"


def other_overlap(text, after, uid, stop=()):
    """这句跟它说完以后、群里别人（不是他自己）说的话最多对上了哪几个词 → (对上的词, 那个人的名字)。
    只看最近 8 句；after：它说完以后的群聊（[{"uid", "name", "text"}]）"""
    mine = bigrams(text, stop)
    best, who = set(), ""
    for h in (after or [])[-8:]:
        if str(h.get("uid")) == str(uid):
            continue
        hit = mine & bigrams(h.get("text") or "", stop)
        if len(hit) > len(best):
            best, who = hit, str(h.get("name") or "")
    return best, who


def calls_other(text, history, uid, names=()):
    """这句开头叫的是不是群里别人的名字（最近说过话的人，不算他自己、不算机器人）→ 那个名字 / ""。
    名字按群名片去掉后面的数字算、至少两个字；跟机器人的名字有重叠的不算（免得把叫它当成叫别人）"""
    lead = re.sub(r"^[@\s　]+", "", text or "")
    if not lead:
        return ""
    mine = [x for x in (names or ()) if x]
    cand = {
        short_name(h.get("name") or "")
        for h in history or ()
        if h.get("role") == "user" and str(h.get("uid")) != str(uid)
    }
    for n in sorted(cand, key=len, reverse=True):
        if len(n) < 2 or any(n in x or x in n for x in mine):
            continue
        if lead.startswith(n):
            return n
    return ""


# ═════════════════════ 接话 ═════════════════════
class Followup:
    def __init__(self, conf=None, rand=None):
        self.c = conf or Conf()
        self.rand = rand or random.random
        self.g = {}

    def st(self, g):
        return self.g.setdefault(
            str(g), {"t0": 0.0, "sent": 0.0, "text": "", "answered": set(), "turns": {}}
        )

    def note_turn(self, g, text, answered=(), t0=None, sent=None):
        """机器人在这个群说完一轮话（t0 = 它开始想这一轮的时候；answered = 这一轮在回的人）"""
        sent = float(sent or time.time())
        self.st(g).update(
            t0=float(t0 or sent),
            sent=sent,
            text=str(text or ""),
            answered={str(u) for u in answered or () if str(u)},
        )

    def reset(self, g, uid=None):
        """有人 @ 它 / 引用它：这个人的接话轮数重新算"""
        t = self.st(g)["turns"]
        if uid is None:
            t.clear()
        else:
            t.pop(str(uid), None)

    def cap(self, is_owner=False):
        return int(self.c("owner_max_turns") if is_owner else self.c("max_turns"))

    def turns(self, g, uid, now=None):
        """这个人现在连着被接了几轮（太久没接的清零）"""
        now = now or time.time()
        t = self.st(g)["turns"]
        cur = t.get(str(uid))
        if cur and now - cur[1] > float(self.c("turn_reset_min")) * 60:
            t.pop(str(uid), None)
            return 0
        return cur[0] if cur else 0

    def capped(self, g, uid, now=None, is_owner=False):
        return self.turns(g, uid, now) >= self.cap(is_owner)

    def took(self, g, uid, now=None):
        """决定接这条了：记一轮。返回这是第几轮（顺手清掉早就该清零的人，不然这张表只涨不跌）"""
        now = now or time.time()
        t = self.st(g)["turns"]
        ttl = float(self.c("turn_reset_min")) * 60
        for u in [u for u, v in t.items() if now - v[1] > ttl]:
            t.pop(u, None)
        n = (t.get(str(uid)) or [0, 0])[0] + 1
        t[str(uid)] = [n, now]
        return n

    def check(
        self, g, uid, name, text, now, history, only_pic=False, is_owner=False, names=()
    ):
        """这条没点名的群消息 是不是在接它的话。返回 (结论, 原因, 分数)；结论 yes / no / ask
        history：这条之前的群聊（[{"role": "user"/"assistant", "uid", "name", "text", "ts"}]，不含这一条）
        names：机器人的名字和外号（说到了算叫它）"""
        if not self.c("enable") or not self.c("follow_enable"):
            return "no", "", 0.0
        st = self.st(g)
        if not st["sent"]:
            return "no", "", 0.0  # 它还没在这个群说过话
        my_text, t0, t_me, answered = st["text"], st["t0"], st["sent"], st["answered"]
        dt = now - t_me
        if dt > float(self.c("window_sec")) or dt < -5:
            return "no", "", 0.0
        uid = str(uid)
        if self.c("closing_skip") and is_closing(text):
            return "no", "收尾句（好了 / 就这样 / 睡了）不接", 0.0
        n = self.turns(g, uid, now)
        if n >= self.cap(is_owner):
            return "no", f"不 @ 它已经连着接了 {n} 轮（要 @ 它才接）", 0.0

        after = [
            h
            for h in history or []
            if h.get("role") == "user" and float(h.get("ts") or 0) > t0
        ]
        others = [h for h in after if str(h.get("uid")) != uid]
        first = not any(str(h.get("uid")) == uid for h in after)
        names = [x for x in (names or ()) if x and len(x) >= 2]
        stop = set().union(*(bigrams(x) for x in names)) if names else set()

        s, why = 0.0, []
        if uid in answered:
            s += 2
            why.append("它刚回的就是他")
        nick = short_name(name)
        if nick and len(nick) >= 2 and nick in my_text:
            s += 2
            why.append("它刚点了他的名")
        if names and any(x in text for x in names):
            s += 2
            why.append("叫了它的名字")
        if "你们" in text:
            s += 0.5
        elif "你" in text:
            s += 1.5
            why.append("说的是「你」")
        if QUESTION.search(text):
            s += 1
            why.append("在问")
        common = bigrams(text, stop) & bigrams(my_text, stop)
        if common:
            s += 2.5 if len(common) >= 2 else 1
            why.append("接了它的词「" + "、".join(sorted(common)[:3]) + "」")
        if (
            self.c("topic_overlap")
            and uid not in answered
            and len(common) < 2
            and not (names and any(x in text for x in names))
        ):
            pen = float(self.c("topic_penalty"))
            s -= pen
            why.append("话题词没对上两个")
        # 跟群里别人刚说的话对得上、比跟它的话还像 → 多半在回那个人（v0.2.0：甲说「修吧，弄好再说」，
        # 乙接着说「你已经说过好几遍修好再说了」是冲甲的，以前它刚回过乙、句子里又有「你」，就当成在跟它说了）
        if self.c("other_overlap"):
            hit, by = other_overlap(text, after, uid, stop)
            if len(hit) >= 2 and len(hit) > len(common):
                s -= float(self.c("other_penalty"))
                why.append(
                    f"跟{(by or '别人')[:8]}刚说的对上了「"
                    + "、".join(sorted(hit)[:3])
                    + "」多半在回他"
                )
        if dt <= 30:
            s += 1
            why.append(f"紧跟着它 {int(max(dt, 0))} 秒")
        elif dt <= 90:
            s += 0.5
        if first:
            s += 0.5
        if len(others) >= 2:
            s -= 1.5
            why.append(f"中间有别人插了 {len(others)} 句")
        # 开头叫的是群里别人的名字（「小李来段语音」）→ 多半在跟那个人说（v0.2.0：以前算「它刚回的就是他 + 接了它的词」就接了）
        callee = calls_other(text, history, uid, names) if self.c("call_other") else ""
        if callee:
            s -= float(self.c("call_other_penalty"))
            why.append(f"开头叫的是「{callee[:8]}」多半在跟他说")
        if any(
            f"@{x}" in (h.get("text") or "") for h in others for x in {name, nick} if x
        ):
            s -= 2.5
            why.append("别人刚 @ 了他 多半在回那个人")
        if only_pic:
            s -= 1.5
            why.append("只发了图")
        reason = "、".join(why) or "没什么信号"
        if s >= float(self.c("yes_score")):
            if only_pic and self.rand() >= float(self.c("pic_prob")):
                return "no", reason + "（图 这次不接）", s
            return "yes", reason, s
        if s <= float(self.c("no_score")) or only_pic or not self.c("ask_model"):
            return "no", reason, s
        return "ask", reason, s


JUDGE_SYSTEM = (
    "你在旁观一个 QQ 群。群里有个成员叫「{bot}」（是个 AI）。判断最后一条消息是不是在跟{bot}说话："
    "接{bot}刚才的话、回答它、反问它、跟它抬杠、叫它做事 都算「是」；"
    "在跟别人说话、跟大家说、自言自语、只是发个反应 算「否」。只回答一个字：是 或 否"
)


def judge_system(bot):
    return JUDGE_SYSTEM.format(bot=bot or "机器人")


def judge_body(history, name, text, now, bot="机器人"):
    """让模型判断时给它看的：最近 8 条 + 要判断的这一条"""
    lines = []
    for h in [h for h in (history or []) if h.get("ts")][-8:]:
        who = (
            (bot or "机器人")
            if h.get("role") == "assistant"
            else (h.get("name") or "某人")
        )
        if h.get("re"):
            who += f"（回复 {h['re']}）"
        lines.append(
            f"[{ago(now - float(h['ts']))}] {who}: {(h.get('text') or '')[:120]}"
        )
    lines.append(f"[刚刚] {name}: {(text or '')[:160]}   ← 判断这一条")
    return "\n".join(lines)


def judge_yes(out):
    """模型的回答 → 是不是在跟它说（「不是」「否」算不是；看不懂也算不是）"""
    t = re.sub(r"<think>.*?</think>", "", str(out or ""), flags=re.S)
    t = t.strip().strip("「」『』\"'*`（）()[]【】 。.!！:：")
    if not t:
        return False
    if t.startswith(("不是", "否", "不", "没有", "no", "No", "NO")):
        return False
    if t.startswith(("是", "对", "yes", "Yes", "YES")):
        return True
    head = t[:6]
    return "是" in head and "不是" not in head and "否" not in head


# ═════════════════════ 在线一阵 ═════════════════════
class Rhythm:
    def __init__(self, conf=None, rand=None, uniform=None):
        self.c = conf or Conf()
        self.rand = rand or random.random
        self.uniform = uniform or random.uniform
        self.g = {}

    def enabled(self):
        return bool(self.c("enable")) and bool(self.c("rhythm_enable"))

    def st(self, g):
        return self.g.setdefault(
            str(g),
            {
                "mode": "lurk",
                "since": 0.0,
                "until": 0.0,
                "last_other": 0.0,
                "last_me": 0.0,
                "free": 0,
                "probed": 0.0,
                "cool_until": 0.0,
                "last_try": 0.0,
                "lines": [],
            },
        )

    def heard(self, g, now=None):
        """群里有人（不是它）说话了"""
        self.st(g)["last_other"] = now or time.time()

    def spoke(self, g, addressed, now=None):
        """它在群里回了一句。addressed = 有人找它（@ / 引用 / 接它的话）。返回 "enter"（刚进在线）或 None"""
        if not self.enabled():
            return None
        now = now or time.time()
        st = self.st(g)
        st["last_me"] = now
        st["probed"] = 0.0
        if st["mode"] != "active":
            if now < st["cool_until"]:
                return None  # 刚收尾：被 @ 回了一句 也不重新进在线
            lo, hi = sorted((float(self.c("online_min")), float(self.c("online_max"))))
            st.update(
                mode="active", since=now, until=now + self.uniform(lo, hi) * 60, free=0
            )
            return "enter"
        if addressed:  # 有人一直找它 就多待一会儿（有上限）
            cap = st["since"] + float(self.c("max_minutes")) * 60
            st["until"] = min(cap, max(st["until"], now + 5 * 60))
        else:
            st["free"] += 1
        return None

    def is_active(self, g):
        return self.enabled() and self.st(g)["mode"] == "active"

    def cooling(self, g, now=None):
        return (now or time.time()) < self.st(g)["cool_until"]

    def minutes_in(self, g, now=None):
        st = self.st(g)
        return (
            int(((now or time.time()) - st["since"]) / 60)
            if st["mode"] == "active"
            else 0
        )

    def chat_block(self, g, now=None, to_other=False):
        """在线时挑着接的硬规矩（不掷骰子；v0.4.0 拆出来，接话打分生效时也用）→ 不能接的原因 / ""（能接）"""
        now = now or time.time()
        st = self.st(g)
        if not self.enabled() or st["mode"] != "active":
            return "不在线"
        if to_other:
            return "在跟别人说"
        if now - max(st["last_me"], st["last_try"]) < float(self.c("min_gap_sec")):
            return f"离上次说话不到 {int(float(self.c('min_gap_sec')))} 秒"
        if st["free"] >= int(self.c("max_replies")):
            return f"这次在线已经挑着接了 {st['free']} 次"
        return ""

    def note_try(self, g, now=None):
        """想接（哪怕最后它选择不说）也算一次 免得连着想"""
        self.st(g)["last_try"] = now or time.time()

    def want_reply(self, g, to_other=False, now=None, only_pic=False):
        """在线时 一条没点名的消息 要不要挑着接"""
        now = now or time.time()
        if self.chat_block(g, now, to_other):
            return False
        p = float(self.c("reply_prob")) * (
            float(self.c("pic_factor")) if only_pic else 1.0
        )
        if self.rand() < p:
            self.note_try(g, now)
            return True
        return False

    def tick(self, g, now=None):
        """后台定时看一眼。返回要做的事：None / "probe"（冷场试探）/ "exit"（收尾潜水）/ "quiet"（默默回观望）"""
        if not self.enabled():
            return None
        now = now or time.time()
        st = self.st(g)
        if st["mode"] != "active":
            return None
        last_any = max(st["last_other"], st["last_me"])
        if now >= st["until"]:
            recent = (
                now - st["last_other"] <= float(self.c("exit_if_chat_within_min")) * 60
            )
            self.to_lurk(g, now)
            if (
                recent
                and self.c("exit_line")
                and self.rand() < float(self.c("exit_line_prob"))
            ):
                return "exit"
            return "quiet"
        if st["probed"]:
            if st["last_other"] > st["probed"]:
                st["probed"] = 0.0  # 有人接话了 接着聊
            elif now - st["probed"] >= float(self.c("probe_wait_min")) * 60:
                self.to_lurk(g, now, cool=False)  # 试探了也没人理 安静回观望
                return "quiet"
            else:
                return None
        if now - last_any >= float(self.c("idle_min")) * 60:
            if self.rand() < float(self.c("probe_prob")):
                st["probed"] = now
                return "probe"
            self.to_lurk(g, now, cool=False)
            return "quiet"
        return None

    def to_lurk(self, g, now=None, cool=True):
        now = now or time.time()
        st = self.st(g)
        st["mode"] = "lurk"
        st["probed"] = 0.0
        if cool:
            st["cool_until"] = max(
                st["cool_until"], now + float(self.c("cooldown_min")) * 60
            )

    def uncool(self, g):
        self.st(g)["cool_until"] = 0.0

    def active_groups(self):
        return [g for g, st in self.g.items() if st["mode"] == "active"]

    # 主动说话（试探 / 收尾）的每小时上限
    def line_ok(self, g, now=None):
        now = now or time.time()
        st = self.st(g)
        st["lines"] = [t for t in st["lines"] if now - t < 3600]
        return len(st["lines"]) < int(self.c("lines_per_hour"))

    def note_line(self, g, now=None):
        self.st(g)["lines"].append(now or time.time())

    def status(self, g, now=None):
        """给面板看的：观望 / 在线（还剩几分钟）/ 收尾冷却"""
        now = now or time.time()
        st = self.st(g)
        d = {
            "mode": "观望",
            "left_sec": 0,
            "in_sec": 0,
            "free": st["free"],
            "cool_sec": 0,
            "probed": bool(st["probed"]),
            "last_other": st["last_other"],
            "last_me": st["last_me"],
        }
        if st["mode"] == "active":
            d.update(
                mode="在线",
                left_sec=max(0, int(st["until"] - now)),
                in_sec=max(0, int(now - st["since"])),
            )
        elif now < st["cool_until"]:
            d.update(mode="收尾冷却", cool_sec=int(st["cool_until"] - now))
        return d


# ═════════════════════ 给模型的话 ═════════════════════
def follow_hint(why):
    why = (why or "紧跟着你说的").replace(
        "它", "你"
    )  # 原因是从旁边看的（「它刚回的就是他」），给机器人看换成「你」
    return (
        f"[群聊节奏] 这句没 @ 你，但看起来是在接你刚才的话（{why}）。自然地接上就行；"
        f"如果其实不是在跟你说，只回 {SILENT}。"
    )


# 有人 @ 它说欢迎 = 在欢迎它（v0.1.1：机器人刚被拉进群，群友 @ 它说「欢迎新人！」，它反过来回了一句「欢迎欢迎，XX 进群啦～」——
# AstrBot 把 @ 它的那一段从消息里去掉了，模型只看到「XX：欢迎新人！」，不知道这句是冲它来的）
WELCOME = re.compile(
    r"欢迎(?:欢迎)?(?:一下)?(?:新人|新朋友|新成员|新来的|萌新|新同学|新鱼|加入|进群|入群|来到|光临|你|回来)"
    r"|新人(?:报到|好)|^(?:欢迎)+[!！~～。.\s]*$"
)


def is_welcome(text):
    return bool(WELCOME.search(str(text or "").strip()))


def welcome_hint(name):
    who = short_name(name) or "他"
    return (
        f"[群聊节奏] {who} 这句 @ 了你、在说欢迎：是在欢迎你进群（你是刚来的那个，不是{who}）。"
        "谢谢他、一句话说你是谁就够；别反过来欢迎他。"
    )


def chat_hint(minutes):
    return (
        f"[群聊节奏] 你正在群里跟大家聊着（已经聊了 {int(minutes)} 分钟）。这句没点你名："
        f"能自然接就接一句；不是跟你说的、或者没什么好说的，只回 {SILENT}。"
    )


PROBE_SCENE = (
    "群里刚才还在聊（你也在聊），现在突然安静了几分钟。"
    "你可以顺着刚才的话题再补一句、抛个相关的小问题、或者吐槽一句 —— 就一句，短的，像真人随口一说。"
    f"别专门去点破冷场（问大家去哪了、说群里安静）。没什么想说的就只写 {SILENT}。"
)

EXIT_SCENE = (
    "你在群里已经跟大家聊了一阵子，现在想下线 / 潜水了（去吃饭、去睡觉、去忙别的……随你，看现在几点）。"
    "自然地说一句很短的收尾话，像真人退出聊天那样，可以带一点刚才聊的内容。"
    f"只说一句，别提问，别总结，别说约下次的客套话。不想说就只写 {SILENT}。"
)


def line_system(persona_prompt, act, clock=""):
    """主动说一句（试探 / 收尾）时的 system prompt：人格 + 现在的情况"""
    scene = PROBE_SCENE if act == "probe" else EXIT_SCENE
    head = (persona_prompt or "").strip()
    tail = (f"现在是 {clock}。" if clock else "") + "你在一个 QQ 群里。" + scene
    return (head + "\n\n" + tail) if head else tail


def line_prompt(history, now, act, max_lines=12):
    """主动说一句时给模型看的：最近的群聊（「你」= 它自己说的）+ 一句要求"""
    lines = []
    for h in [h for h in (history or []) if h.get("ts")][-max_lines:]:
        who = "你" if h.get("role") == "assistant" else (h.get("name") or "某人")
        lines.append(
            f"[{ago(now - float(h['ts']))}] {who}: {(h.get('text') or '')[:100]}"
        )
    hint = "（大家突然都不说话了）" if act == "probe" else "（你想下线 / 潜水了）"
    return (
        "群里最近的聊天（「你」是你自己说的）：\n"
        + ("\n".join(lines) or "（没记下来）")
        + "\n\n"
        + hint
        + f"\n只写你要发到群里的那一句话，别加引号、别加说明。不想说就只写 {SILENT}。"
    )


def clean_line(text, max_chars=120):
    """模型写的那一句 → 能发的一句（想了想没说 / 空的 → ""）"""
    t = re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.S).strip()
    if not t or SILENT in t or "[[" in t:
        return ""
    line = next((x.strip() for x in t.splitlines() if x.strip()), "")
    line = line.strip("「」\"' ")
    if len(line) > max_chars:
        line = line[:max_chars].rstrip("，,、 ") + "…"
    return line


def is_silent(text):
    return SILENT in str(text or "")


# 模型选了不说（[[不说]]）：这一轮不发、不进对话记录 —— 可这一轮已经把 AstrBot 攒着的群聊上下文
# （「群聊消息记录注入上下文」）用掉了，不补上的话这段在聊什么它就再也看不到了。
# 所以把这一轮本来要存的那些文字 + 这条消息本身留着，这个会话下一次叫模型时补进去（跟着那一轮存进记录）
CARRY_HEAD = "（下面是你上次没接话时群里在聊的，当时你选了不说；只是让你知道前面在聊什么，不用专门回应）\n"
CARRY_MAX = 4000


def carry_block(parts, said="", who="", max_chars=CARRY_MAX):
    """parts：这一轮要存进记录的文字块（按顺序；只给这一轮的提示不算）；said / who：这条消息和谁说的。
    返回下一次要补进去的一段（没东西 → ""）；上次没补上的也在 parts 里，会接着带上；太长只留最后 max_chars 个字"""
    bits = []
    for p in parts or ():
        p = str(p or "")
        if p.startswith(CARRY_HEAD):
            p = p[len(CARRY_HEAD) :]
        if p.strip():
            bits.append(p.strip())
    said = str(said or "").strip()
    if said:
        bits.append(f"{who}: {said}" if who else said)
    if not bits:
        return ""
    body = "\n".join(bits)
    if len(body) > max_chars:
        body = "…" + body[-max_chars:]
    return CARRY_HEAD + body


# ═════════════════════ 今天的数 ═════════════════════
class Daily:
    KEYS = (
        "follow",
        "chat",
        "probe",
        "exit",
        "judge",
        "judge_yes",
        "silent",
        "skip_ai",
        "quiet",  # （v0.5.0）免打扰时段里没接 / 没说的
    )

    def __init__(self, d=None):
        self.d = dict(d) if isinstance(d, dict) else {}

    def today(self, day=None):
        day = day or time.strftime("%Y-%m-%d")
        if self.d.get("day") != day:
            self.d = {"day": day}
        for k in self.KEYS:
            self.d.setdefault(k, 0)
        return self.d

    def add(self, key, n=1, day=None):
        t = self.today(day)
        t[key] = int(t.get(key, 0)) + n
        return t[key]
