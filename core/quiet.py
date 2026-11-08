# -*- coding: utf-8 -*-
"""免打扰时段（v0.5.0）—— 判断逻辑（不依赖 AstrBot，单测能直接用）

配置写「01:00-07:00」这种，可以写几段、可以跨午夜（「23:30-07:00」）；按配置的时区算（默认北京时间）。
这段时间里群聊节奏不主动接话、不挑着接、不冷场试探、不说收尾话，别的插件问「能不能冒泡」也说不行；
被 @ / 引用 / 用唤醒词叫它的照回（另有一项能关）；只叫它名字、没 @ 的不算 —— 平时那种也是群聊节奏打分决定接不接，
免打扰时群聊节奏一律不接（想让「名字开头」也算叫它：把名字加进 AstrBot 的唤醒词）；私聊不受影响。外壳（main.py / rhythm_mod.py）问 state() 就行。

时区写法照「看图懂梗」：中国 1991 年以后没有夏令时，北京时间就是固定的 UTC+8，不用装 tzdata（Windows 上常常没有）；
常见几个名字和「UTC+8」这种直接认，别的名字才去问 zoneinfo，认不出 → 北京时间。
"""

import re
import time
from datetime import datetime, timedelta, timezone

_FIXED_TZ = {
    "asia/shanghai": 8,
    "asia/chongqing": 8,
    "asia/harbin": 8,
    "asia/urumqi": 8,
    "prc": 8,
    "asia/hong_kong": 8,
    "asia/macau": 8,
    "asia/taipei": 8,
    "asia/singapore": 8,
    "asia/tokyo": 9,
    "asia/seoul": 9,
    "utc": 0,
    "gmt": 0,
    "etc/utc": 0,
}
_OFFSET = re.compile(r"^(?:utc|gmt)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$")
BEIJING = timezone(timedelta(hours=8), "北京时间")
DAY = 24 * 60


def make_tz(name):
    """配置里的时区 → (tzinfo, 怎么叫它, 认没认出来)
    留空 → (None, 「服务器时间」, True)：跟服务器；认不出 → 北京时间"""
    s = str(name or "").strip()
    if not s:
        return None, "服务器时间", True
    k = s.lower().replace(" ", "")
    if k in _FIXED_TZ:
        off = _FIXED_TZ[k]
        if off == 8:
            return BEIJING, "北京时间", True
        return timezone(timedelta(hours=off), s), s, True
    m = _OFFSET.match(k)
    if m:
        h, mi = int(m.group(2)), int(m.group(3) or 0)
        if h <= 14 and mi < 60:
            sign = 1 if m.group(1) == "+" else -1
            label = f"UTC{m.group(1)}{h}" + (f":{mi:02d}" if mi else "")
            off = timedelta(hours=h, minutes=mi) * sign
            if off == timedelta(hours=8):
                return BEIJING, "北京时间", True
            return timezone(off, label), label, True
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(s), s, True
    except Exception:  # noqa: BLE001  Windows 没装 tzdata / 写错了
        return BEIJING, "北京时间", False


_SPAN = re.compile(
    r"^\s*(\d{1,2})(?:[:：](\d{2}))?\s*(?:-|~|～|—|–|到|至)\s*(\d{1,2})(?:[:：](\d{2}))?\s*$"
)


def _items(spec):
    if isinstance(spec, str):
        spec = re.split(r"[,，;；、\n]+", spec)
    return [str(x).strip() for x in (spec or []) if str(x or "").strip()]


def parse(spec):
    """["01:00-07:00", "23:30-02:00", "13-14"] → ([(开始分钟, 结束分钟)], [看不懂的那几段])。
    结束比开始早 = 跨午夜；「00:00-24:00」= 全天；开始 = 结束的不要（不知道是全天还是一分钟都没有）"""
    out, bad = [], []
    for s in _items(spec):
        m = _SPAN.match(s)
        if not m:
            bad.append(s)
            continue
        a = int(m.group(1)) * 60 + int(m.group(2) or 0)
        b = int(m.group(3)) * 60 + int(m.group(4) or 0)
        if b == 0 and a > 0:  # 「22:00-00:00」= 到半夜十二点
            b = DAY
        if not (0 <= a < DAY and 0 < b <= DAY and int(m.group(2) or 0) < 60 and int(m.group(4) or 0) < 60) or a == b:
            bad.append(s)
            continue
        out.append((a, b))
    return out, bad


def hm(m):
    m = int(m) % (DAY + 1)
    return f"{m // 60:02d}:{m % 60:02d}"


def span_text(a, b):
    return f"{hm(a)}-{hm(b)}"


def minute_of(now=None, tz=None):
    """这个时刻在那个时区是一天里的第几分钟（带小数）；tz = None 按服务器的时间"""
    ts = time.time() if now is None else float(now)
    d = datetime.fromtimestamp(ts, tz) if tz is not None else datetime.fromtimestamp(ts)
    return d.hour * 60 + d.minute + d.second / 60.0


def state(now=None, spans=(), tz=None):
    """现在在不在免打扰 → {"on", "span": 「01:00-07:00」, "left_min": 还有几分钟结束}"""
    m = minute_of(now, tz)
    for a, b in spans or ():
        if a < b:
            on, left = a <= m < b, b - m
        else:  # 跨午夜：23:30-07:00
            on = m >= a or m < b
            left = (b - m) if m < b else (DAY - m + b)
        if on:
            return {"on": True, "span": span_text(a, b), "left_min": max(1, int(round(left)))}
    return {"on": False, "span": "", "left_min": 0}


def stop_why(st, private=False, kind=None, woken=False):
    """要叫模型之前：免打扰时段里这一轮拦不拦 → 原因 / ""（不拦）。
    私聊不管；有人找它（@ / 引用 / 唤醒词，没写 fatfish_kind 又被唤醒的）照回，「免打扰时被 @ 也不回」开着才拦；
    没人找它的（群聊节奏 / 别的插件标了 follow、chat、extra，AstrBot 自带的主动回复）都拦"""
    if private or not (st or {}).get("on"):
        return ""
    if not kind and woken:
        return "「免打扰时被 @ 也不回」开着" if st.get("at_too") else ""
    return "没人找它，免打扰时不主动说"


def ask_why(st, kind="extra", conv="", where=""):
    """别的插件主动说话之前问 ask()：免打扰时段里，群里主动说（冒泡 / 插话 / 接话）说不行 → 原因 / ""。
    私聊、有人找它的不管；会话没给的，看 where 是不是「群 …」"""
    if str(kind) in ("private", "direct") or not (st or {}).get("on"):
        return ""
    conv = str(conv or "")
    group = ":GroupMessage:" in conv or (not conv and str(where or "").startswith("群"))
    if not group:
        return ""
    return f"免打扰时段（{st.get('span', '')} {st.get('tz', '')}）".replace(" ）", "）")
