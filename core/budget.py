# -*- coding: utf-8 -*-
"""
省 token：花得越多 回得越少（不依赖 AstrBot：单测、以后 NapCat 版都能直接用）

  回复意愿  w(u) = 底线 + (1 - 底线) / (1 + (u / u半)^k)

  u     最近 window_hours 小时花了多少钱（美元，按配置的价格折算）
  u半   花到这么多 意愿就掉到一半左右（half_usd，默认 0.45）
  k     形状，小于 1：一开始掉得快，之后越来越慢（shape，默认 0.6）
  底线  再怎么花也留着的意愿（floor，默认 0.1）

  例（half_usd 0.3、shape 0.6、floor 0.1）：花 $0.05 → 77%  $0.1 → 69%  $0.3 → 55%  $1 → 39%  $5 → 24%

不同场合用同一条曲线的不同次方（次方越小越不受影响；0 = 永远回）：
  private 私聊 0 · direct 找它（@ / 引用 / 唤醒词）0.1 · follow 接话 2.5 · chat 插话 3 · extra 冒泡/主动 3
管理员、「不限的人」不掷；火力全开时谁都不掷。
同一个人在同一个会话被跳过以后又来找（私聊 / 找它 / 接话）：每被跳过一次 概率开一次平方根（越执着越容易回），回了就清零。
「重新算」：花费只从那一刻算起（流水不动）；过了统计窗口自动失效。

记账：每次模型回完记 token —— 输入（没命中缓存 / 命中缓存）、输出，按价格折成美元；
DeepSeek 峰谷价：工作日 UTC 01:00–04:00、06:00–10:00（北京时间 9–12、14–18 点）价格翻倍（可以关）。
流水按分钟攒成一格，只留统计窗口（至少 24 小时）里的；统计窗口最多 7 天（168 小时），流水最多 7 × 1440 格。
"""

import math
import random
import re
import time

KINDS = ("private", "direct", "follow", "chat", "extra")
KIND_NAME = {
    "private": "私聊",
    "direct": "找它",
    "follow": "接话",
    "chat": "插话",
    "extra": "冒泡/主动",
}
BOOST_KINDS = (
    "private",
    "direct",
    "follow",
)  # 这几种是「人来找它」：被跳过以后又来找 → 开方
DEFAULTS = {
    "enable": True,
    "half_usd": 0.45,
    "shape": 0.6,
    "floor": 0.1,
    "window_hours": 24,
    "power_private": 0.0,
    "power_direct": 0.1,
    "power_follow": 2.5,
    "power_chat": 3.0,
    "power_extra": 3.0,
    "skip_window_min": 15,
    "price_miss": 0.15,
    "price_hit": 0.003,
    "price_out": 0.60,
    "peak_enable": True,
    "peak_mult": 2.0,
    "peak_hours_utc": ["01:00-04:00", "06:00-10:00"],
    "peak_free_days": [],
    "full_default_hours": 3.0,
    "free_users": [],
}
# 一分钟一格的流水：[输入没命中$, 命中$, 输出$, 输入没命中tok, 命中tok, 输出tok, 回话次数]
SLOTS = 7
MAX_SKIP_PEOPLE = 300
MAX_RECENT = 40
MAX_WINDOW_HOURS = 168.0  # 统计窗口最多 7 天：流水一分钟一格，窗口再大存档就太大了（每次存都要整个写一遍）


def _f(v, d):
    try:
        x = float(v)
        return x if math.isfinite(x) else d
    except (TypeError, ValueError):
        return d


def _list(v):
    if isinstance(v, str):
        v = v.replace("，", ",").replace("、", ",").split(",")
    return [str(x).strip() for x in (v or []) if str(x).strip()]


# ───────────── token 用量：认几种格式 ─────────────
def _get(o, k):
    if o is None:
        return None
    if isinstance(o, dict):
        return o.get(k)
    v = getattr(o, k, None)
    if v is None:
        extra = getattr(
            o, "model_extra", None
        )  # openai 的 pydantic 对象：DeepSeek 多给的字段在这里
        if isinstance(extra, dict):
            v = extra.get(k)
    return v


def _int(v):
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def parse_usage(u):
    """一次调用的用量 → (输入没命中缓存, 输入命中缓存, 输出)；认不出 → None
    认得：DeepSeek（prompt_cache_hit_tokens / prompt_cache_miss_tokens）、
          OpenAI（prompt_tokens + prompt_tokens_details.cached_tokens）、AstrBot 的 TokenUsage（input_other / input_cached / output）"""
    if u is None:
        return None
    hit, miss = _get(u, "prompt_cache_hit_tokens"), _get(u, "prompt_cache_miss_tokens")
    if hit is not None or miss is not None:
        hit = _int(hit)
        miss = (
            _int(miss)
            if miss is not None
            else max(0, _int(_get(u, "prompt_tokens")) - hit)
        )
        return miss, hit, _int(_get(u, "completion_tokens"))
    if _get(u, "prompt_tokens") is not None or _get(u, "completion_tokens") is not None:
        prompt = _int(_get(u, "prompt_tokens"))
        cached = min(
            prompt, _int(_get(_get(u, "prompt_tokens_details"), "cached_tokens"))
        )
        return prompt - cached, cached, _int(_get(u, "completion_tokens"))
    if any(_get(u, k) is not None for k in ("input_other", "input_cached", "output")):
        return (
            _int(_get(u, "input_other")),
            _int(_get(u, "input_cached")),
            _int(_get(u, "output")),
        )
    return None


def pick_kind(extra_kind=None, private=False, woken=False):
    """这次算哪种场合：别的插件写了 fatfish_kind 就用它；没写 → 私聊 = private，群里被 @ / 引用 / 唤醒 = direct，
    群里没人找它（比如别的插件让它自己接一句）= chat"""
    if isinstance(extra_kind, str) and extra_kind.strip():
        return extra_kind.strip().lower()
    if private:
        return "private"
    return "direct" if woken else "chat"


def parse_hours(s, default=3.0, most=72.0):
    """火力全开开多久：「」→ 默认小时数；「一直」→ -1；「6」「6小时」「1.5h」「30分钟」→ 小时数（最多 most）；看不懂 → None"""
    s = re.sub(r"\s+", "", str(s or ""))
    if not s:
        return min(most, max(0.1, _f(default, 3.0)))
    if s in ("一直", "一直开", "不关", "永久", "always", "forever"):
        return -1.0
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(个?小时|小時|h|H|hr|分钟|分鐘|分|min|m)?", s)
    if not m:
        return None
    h = float(m.group(1))
    if (m.group(2) or "") in ("分钟", "分鐘", "分", "min", "m"):
        h /= 60
    return min(float(most), h) if h > 0 else None


def _ranges(spec):
    """["01:00-04:00", "6-10"] → [(1.0, 4.0), (6.0, 10.0)]（UTC 钟点，前闭后开）；写错的跳过"""
    out = []
    for s in _list(spec):
        m = re.fullmatch(
            r"\s*(\d{1,2})(?::(\d{2}))?\s*[-~～—–到]\s*(\d{1,2})(?::(\d{2}))?\s*", s
        )
        if not m:
            continue
        a = int(m.group(1)) + int(m.group(2) or 0) / 60
        b = int(m.group(3)) + int(m.group(4) or 0) / 60
        if 0 <= a < b <= 24:
            out.append((a, b))
    return out


def money(u):
    """美元的说法：钱少的时候多留几位（一次回话往往才几分之一美分）"""
    u = float(u or 0)
    if u == 0:
        return "$0"
    return f"${u:.4f}" if u < 0.01 else f"${u:.3f}" if u < 1 else f"${u:.2f}"


def _hm(h):
    h = h % 24 if h < 24 else 24
    return f"{int(h):02d}:{int(round(h % 1 * 60)):02d}"


class Budget:
    """花费流水 + 回复意愿 + 掷骰子 + 火力全开 + 重新算。时间、随机数都能传进来（方便测）"""

    def __init__(self, cfg=None, state=None, rng=None):
        self.cfg = dict(DEFAULTS)
        self.rng = rng or random.Random()
        self.ledger = {}  # 分钟 -> [SLOTS 个数]
        self.full = None  # {"until": 时间戳 / -1 一直, "since", "by"}
        self.reset = None  # {"ts", "base": [那一分钟已经花了的], "by"}
        self.day = {}  # 今天：{"day", "pass": {场合: 次}, "skip": {场合: 次}, "admin", "full"}
        self.skips = {}  # "会话|QQ" -> {"name", "where", "ts": [最近被跳过的时间], "day", "n", "last"}
        self.recent = []  # 最近的决定
        self.configure(cfg)
        self.load(state)

    # ───────── 配置 ─────────
    def configure(self, cfg=None):
        for k, v in (cfg or {}).items():
            if k not in DEFAULTS or str(k).startswith("_"):
                continue
            d = DEFAULTS[k]
            if isinstance(d, bool):
                self.cfg[k] = bool(v)
            elif isinstance(d, (int, float)):
                self.cfg[k] = _f(v, d)
            elif isinstance(d, list):
                self.cfg[k] = _list(v)
            else:
                self.cfg[k] = v
        c = self.cfg
        c["floor"] = min(1.0, max(0.0, c["floor"]))
        c["shape"] = max(0.05, c["shape"])
        c["window_hours"] = min(MAX_WINDOW_HOURS, max(1.0, c["window_hours"]))
        c["skip_window_min"] = max(0.0, c["skip_window_min"])
        c["peak_mult"] = max(0.0, c["peak_mult"])
        for k in ("price_miss", "price_hit", "price_out", "half_usd") + tuple(
            "power_" + x for x in KINDS
        ):
            c[k] = max(0.0, c[k])
        self._peak = _ranges(c["peak_hours_utc"])
        return self

    def power(self, kind):
        """这个场合的次方；不认识的场合按 1 算"""
        return self.cfg.get("power_" + str(kind), 1.0) if kind in KINDS else 1.0

    # ───────── 曲线 ─────────
    def curve(self, u, half=None, shape=None, floor=None):
        """回复意愿 0~1"""
        half = self.cfg["half_usd"] if half is None else float(half)
        shape = self.cfg["shape"] if shape is None else float(shape)
        floor = self.cfg["floor"] if floor is None else float(floor)
        u = max(0.0, float(u))
        if half <= 0:
            return 1.0
        try:
            x = (u / half) ** shape
        except OverflowError:
            x = math.inf
        return floor + (1.0 - floor) / (1.0 + x)

    # ───────── 峰谷价 ─────────
    def is_peak(self, t=None):
        """这个时刻是不是 DeepSeek 的高峰（UTC 工作日的那几个钟点；节假日除外）"""
        if not self.cfg["peak_enable"]:
            return False
        g = time.gmtime(time.time() if t is None else t)
        if g.tm_wday > 4 or time.strftime("%Y-%m-%d", g) in self.cfg["peak_free_days"]:
            return False
        h = g.tm_hour + g.tm_min / 60 + g.tm_sec / 3600
        return any(a <= h < b for a, b in self._peak)

    def peak_text(self, t=None):
        t = time.time() if t is None else t
        if not self.is_peak(t):
            return "空闲时段（原价）"
        g = time.gmtime(t)
        h = g.tm_hour + g.tm_min / 60 + g.tm_sec / 3600
        end = next((b for a, b in self._peak if a <= h < b), h)
        return f"高峰时段（价格 ×{self.cfg['peak_mult']:g}，UTC {_hm(end)} 结束）"

    # ───────── 记账 ─────────
    def cost(self, miss, hit, out, t=None):
        """→ (没命中$, 命中$, 输出$)"""
        m = self.cfg["peak_mult"] if self.is_peak(t) else 1.0
        c = self.cfg
        return (
            miss * c["price_miss"] / 1e6 * m,
            hit * c["price_hit"] / 1e6 * m,
            out * c["price_out"] / 1e6 * m,
        )

    def record(self, miss, hit, out, t=None, calls=1):
        """记一次（或一轮）调用 → 花了多少美元"""
        t = time.time() if t is None else t
        miss, hit, out = _int(miss), _int(hit), _int(out)
        if not (miss or hit or out or calls):
            return 0.0
        um, uh, uo = self.cost(miss, hit, out, t)
        b = self.ledger.setdefault(int(t // 60), [0.0] * SLOTS)
        for i, v in enumerate((um, uh, uo, miss, hit, out, calls)):
            b[i] += v
        self.trim(t)
        return um + uh + uo

    def keep_hours(self):
        return max(24.0, self.cfg["window_hours"])

    def trim(self, now=None):
        """流水只留统计窗口（至少 24 小时）里的；过期的火力全开 / 重新算 / 跳过记录也清掉"""
        now = time.time() if now is None else now
        cut = int((now - self.keep_hours() * 3600) // 60)
        for m in [m for m in self.ledger if m < cut]:
            del self.ledger[m]
        if self.full and not self.full_state(now)["on"]:
            self.full = None
        if self.reset and not self.reset_state(now):
            self.reset = None
        sw = self.cfg["skip_window_min"] * 60
        today = self._daykey(now)
        for k in list(self.skips):
            s = self.skips[k]
            s["ts"] = [x for x in s.get("ts") or [] if now - x < sw]
            if s.get("day") != today:
                s["day"], s["n"] = today, 0
            if not s["ts"] and not s.get("n"):
                del self.skips[k]
        if len(self.skips) > MAX_SKIP_PEOPLE:
            for k in sorted(self.skips, key=lambda k: self.skips[k].get("last") or 0)[
                : len(self.skips) - MAX_SKIP_PEOPLE
            ]:
                del self.skips[k]

    def sums(self, now=None, hours=None, honor_reset=True):
        """最近 hours 小时（默认统计窗口）的流水加起来：[没命中$, 命中$, 输出$, 没命中tok, 命中tok, 输出tok, 次数]
        honor_reset：点过「重新算」就只算点了以后的（点的那一分钟减掉点的时候已经花了的）"""
        now = time.time() if now is None else now
        start = now - float(hours or self.cfg["window_hours"]) * 3600
        r = self.reset_state(now) if honor_reset else None
        rm = int(r["ts"] // 60) if r else None
        tot = [0.0] * SLOTS
        for m, b in self.ledger.items():
            if (m + 1) * 60 <= start:
                continue
            if r:
                if m < rm:
                    continue
                if m == rm:
                    b = [max(0.0, x - y) for x, y in zip(b, r["base"])]
            for i in range(SLOTS):
                tot[i] += b[i]
        return tot

    def spent(self, now=None):
        """曲线用的花费（美元）：统计窗口里的；点过「重新算」只算之后的"""
        s = self.sums(now)
        return s[0] + s[1] + s[2]

    def hourly(self, now=None, hours=24):
        """最近 hours 个小时每小时花了多少（老 → 新；不管「重新算」）"""
        now = time.time() if now is None else now
        top = int(now // 3600)
        out = [0.0] * int(hours)
        for m, b in self.ledger.items():
            i = int(hours) - 1 - (top - m * 60 // 3600)
            if 0 <= i < len(out):
                out[i] += b[0] + b[1] + b[2]
        return out

    # ───────── 火力全开 ─────────
    def set_full(self, hours, by="", now=None):
        """hours > 0：开这么多小时；< 0：一直开；0：关"""
        now = time.time() if now is None else now
        h = _f(hours, 0.0)
        self.full = (
            None
            if h == 0
            else {
                "until": -1 if h < 0 else now + h * 3600,
                "since": now,
                "by": str(by)[:20],
            }
        )
        return self.full_state(now)

    def full_state(self, now=None):
        now = time.time() if now is None else now
        f = self.full or {}
        until = _f(f.get("until"), 0.0)
        on = until < 0 or until > now
        return {
            "on": on,
            "until": until if on else 0,
            "since": _f(f.get("since"), 0.0) if on else 0,
            "left": (-1 if until < 0 else int(until - now)) if on else 0,
            "by": (f.get("by") or "") if on else "",
        }

    @staticmethod
    def full_text(st):
        if not st["on"]:
            return "没开"
        if st["left"] < 0:
            return "一直开着（要手动关）"
        m = max(1, int(st["left"]) // 60)
        return f"还剩 {m // 60} 小时 {m % 60} 分钟" if m >= 60 else f"还剩 {m} 分钟"

    # ───────── 重新算 ─────────
    def reset_now(self, by="", now=None):
        now = time.time() if now is None else now
        self.reset = {
            "ts": now,
            "base": list(self.ledger.get(int(now // 60), [0.0] * SLOTS)),
            "by": str(by)[:20],
        }
        return self.reset_state(now)

    def reset_clear(self):
        self.reset = None

    def reset_state(self, now=None):
        """点过「重新算」而且还在统计窗口里 → {"ts", "base", "by"}；否则 None"""
        r = self.reset or {}
        ts = _f(r.get("ts"), 0.0)
        now = time.time() if now is None else now
        if ts <= 0 or now - ts > self.cfg["window_hours"] * 3600:
            return None
        base = (
            r.get("base")
            if isinstance(r.get("base"), list) and len(r.get("base")) == SLOTS
            else [0.0] * SLOTS
        )
        return {"ts": ts, "base": [_f(x, 0.0) for x in base], "by": r.get("by") or ""}

    # ───────── 掷骰子 ─────────
    def probs(self, now=None):
        w = self.curve(self.spent(now))
        return {k: w ** self.power(k) for k in KINDS}

    @staticmethod
    def _daykey(t):
        return time.strftime("%Y-%m-%d", time.localtime(t))

    def today(self, now=None):
        now = time.time() if now is None else now
        day = self._daykey(now)
        if self.day.get("day") != day:
            self.day = {"day": day, "pass": {}, "skip": {}, "admin": 0, "full": 0}
        return self.day

    def decide(
        self, kind, conv="", uid="", name="", admin=False, now=None, roll=None, where=""
    ):
        """这次回不回 → {"ok", "why", "p", "w", "u", "kind", "boost", "roll", "by"}
        by：admin（管理员 / 不限的人）、off（没开）、full（火力全开）、roll（掷的）"""
        now = time.time() if now is None else now
        uid = str(uid or "")
        kind = str(kind or "chat")
        self.trim(now)
        day = self.today(now)
        d = {
            "kind": kind,
            "p": 1.0,
            "w": 1.0,
            "u": 0.0,
            "boost": 0,
            "roll": None,
            "ok": True,
            "by": "roll",
            "why": "",
        }
        if admin or (uid and uid in self.cfg["free_users"]):
            d.update(by="admin", why="管理员 / 不限的人：不掷")
            day["admin"] = day.get("admin", 0) + 1
        elif not self.cfg["enable"]:
            d.update(by="off", why="省 token 没开：不掷")
        elif self.full_state(now)["on"]:
            d.update(by="full", why="火力全开：不掷")
            day["full"] = day.get("full", 0) + 1
        key = f"{conv}|{uid}"
        if d["by"] != "roll":
            day["pass"][kind] = day["pass"].get(kind, 0) + 1
            if key in self.skips:  # 回了就清零
                self.skips[key]["ts"] = []
            self._note(d, now, name or uid, where)
            return d
        u = self.spent(now)
        w = self.curve(u)
        p = w ** self.power(kind)
        n = (
            len(self.skips.get(key, {}).get("ts") or [])
            if uid and self.cfg["skip_window_min"] > 0
            else 0
        )
        if n and kind in BOOST_KINDS:
            p = p ** (0.5**n)
            d["boost"] = n
        roll = self.rng.random() if roll is None else float(roll)
        ok = roll < p
        win = self.cfg["window_hours"]
        d.update(
            p=p,
            w=w,
            u=u,
            roll=roll,
            ok=ok,
            why=(
                f"最近{win:g}小时花了 {money(u)}"
                + ("（重新算过）" if self.reset_state(now) else "")
                + f" · 意愿 {w:.0%} · {KIND_NAME.get(kind, kind)} {p:.0%}"
                + (f"（被跳过 {n} 次，开了 {n} 次平方根）" if d["boost"] else "")
                + f" · 掷出 {roll:.2f}"
            ),
        )
        if ok:
            day["pass"][kind] = day["pass"].get(kind, 0) + 1
            if key in self.skips:
                self.skips[key]["ts"] = []
        else:
            day["skip"][kind] = day["skip"].get(kind, 0) + 1
            if uid:
                s = self.skips.setdefault(
                    key, {"ts": [], "day": self._daykey(now), "n": 0}
                )
                s["ts"] = (s.get("ts") or []) + [now]
                s.update(
                    name=str(name or uid)[:30],
                    where=str(where)[:40],
                    last=now,
                    n=int(s.get("n") or 0) + 1,
                )
        self._note(d, now, name or uid, where)
        return d

    def _note(self, d, now, who, where):
        self.recent.append(
            {
                "t": int(now),
                "kind": d["kind"],
                "ok": d["ok"],
                "by": d["by"],
                "p": round(d["p"], 4),
                "roll": None if d["roll"] is None else round(d["roll"], 3),
                "who": str(who)[:30],
                "where": str(where)[:40],
                "boost": d["boost"],
            }
        )
        del self.recent[:-MAX_RECENT]

    # ───────── 给面板 / 指令看 ─────────
    def snapshot(self, now=None):
        now = time.time() if now is None else now
        self.trim(now)
        s = self.sums(now)
        s_all = self.sums(now, honor_reset=False)
        u = s[0] + s[1] + s[2]
        w = self.curve(u)
        r = self.reset_state(now)
        c = self.cfg
        return {
            "enable": c["enable"],
            "window_hours": c["window_hours"],
            "spent": u,
            "spent_all": s_all[0] + s_all[1] + s_all[2],
            "parts": {
                "miss": {"usd": s_all[0], "tok": int(s_all[3])},
                "hit": {"usd": s_all[1], "tok": int(s_all[4])},
                "out": {"usd": s_all[2], "tok": int(s_all[5])},
                "calls": int(s_all[6]),
            },
            "will": w,
            "probs": {k: w ** self.power(k) for k in KINDS},
            "powers": {k: self.power(k) for k in KINDS},
            "kind_names": dict(KIND_NAME),
            "half_usd": c["half_usd"],
            "shape": c["shape"],
            "floor": c["floor"],
            "full": self.full_state(now),
            "full_text": self.full_text(self.full_state(now)),
            "full_default_hours": c["full_default_hours"],
            "reset": {"ts": r["ts"], "by": r["by"]} if r else None,
            "today": dict(self.today(now)),
            "peak": {
                "now": self.is_peak(now),
                "text": self.peak_text(now),
                "enable": c["peak_enable"],
                "mult": c["peak_mult"],
                "hours": [f"{_hm(a)}-{_hm(b)}" for a, b in self._peak],
            },
            "prices": {
                "miss": c["price_miss"],
                "hit": c["price_hit"],
                "out": c["price_out"],
            },
            "hourly": [round(x, 9) for x in self.hourly(now)],
            "skips": sorted(
                (
                    {
                        "who": v.get("name") or k.split("|")[-1],
                        "where": v.get("where") or "",
                        "today": v.get("n") or 0,
                        "streak": len(v.get("ts") or []),
                        "last": int(v.get("last") or 0),
                    }
                    for k, v in self.skips.items()
                ),
                key=lambda x: -x["last"],
            )[:30],
            "recent": list(reversed(self.recent))[:30],
        }

    def status_text(self, now=None):
        """给指令回的一段话"""
        st = self.snapshot(now)
        p, t = st["parts"], st["today"]
        kinds = " · ".join(f"{KIND_NAME[k]} {st['probs'][k]:.0%}" for k in KINDS)
        npass, nskip = sum(t["pass"].values()), sum(t["skip"].values())
        lines = [
            f"最近 {st['window_hours']:g} 小时花了 {money(st['spent_all'])}"
            f"（输入 {money(p['miss']['usd'])} · 缓存 {money(p['hit']['usd'])} · 输出 {money(p['out']['usd'])}，{p['calls']} 次回话）"
        ]
        if st["reset"]:
            lines.append(
                f"重新算过：从 {time.strftime('%m-%d %H:%M', time.localtime(st['reset']['ts']))} 起算 {money(st['spent'])}"
            )
        lines.append(f"回复意愿 {st['will']:.0%}：{kinds}")
        lines.append(
            f"火力全开：{st['full_text']}"
            + ("" if st["enable"] else "（省 token 没开：只记账不拦）")
        )
        lines.append(f"今天：回了 {npass} 次 · 跳过 {nskip} 次")
        lines.append(
            f"现在是{st['peak']['text']}" if st["peak"]["enable"] else "峰谷价：关了"
        )
        return "\n".join(lines)

    # ───────── 存档 ─────────
    def dump(self):
        return {
            "v": 1,
            "ledger": [
                [m] + [round(x, 9) for x in b] for m, b in sorted(self.ledger.items())
            ],
            "full": self.full,
            "reset": self.reset,
            "day": self.day,
            "skips": self.skips,
            "recent": self.recent,
        }

    def load(self, d):
        if not isinstance(d, dict):
            return self
        for row in d.get("ledger") or []:
            try:
                if isinstance(row, list) and len(row) == SLOTS + 1:
                    self.ledger[int(row[0])] = [_f(x, 0.0) for x in row[1:]]
            except (TypeError, ValueError):
                continue
        self.full = d.get("full") if isinstance(d.get("full"), dict) else None
        self.reset = d.get("reset") if isinstance(d.get("reset"), dict) else None
        # 下面这些里面的数也要核一遍：哪个数写成了字（手改过、版本不一样），记账 / 掷骰子 / 面板每次都会出错
        day = d.get("day")
        if (
            isinstance(day, dict)
            and isinstance(day.get("pass"), dict)
            and isinstance(day.get("skip"), dict)
        ):
            self.day = {
                "day": str(day.get("day") or ""),
                "pass": {str(k): _int(v) for k, v in day["pass"].items()},
                "skip": {str(k): _int(v) for k, v in day["skip"].items()},
                "admin": _int(day.get("admin")),
                "full": _int(day.get("full")),
            }
        sk = d.get("skips")
        if isinstance(sk, dict):
            self.skips = {}
            for k, v in sk.items():
                if not isinstance(v, dict):
                    continue
                ts = v.get("ts") if isinstance(v.get("ts"), list) else []
                self.skips[str(k)] = {
                    "ts": [x for x in (_f(t, None) for t in ts) if x is not None],
                    "day": str(v.get("day") or ""),
                    "n": _int(v.get("n")),
                    "name": str(v.get("name") or "")[:30],
                    "where": str(v.get("where") or "")[:40],
                    "last": _f(v.get("last"), 0.0),
                }
        rc = d.get("recent")
        if isinstance(rc, list):
            self.recent = [x for x in rc if isinstance(x, dict)][-MAX_RECENT:]
        return self
