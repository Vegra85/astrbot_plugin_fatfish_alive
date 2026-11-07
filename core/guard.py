# -*- coding: utf-8 -*-
"""兜底的判断（不依赖 AstrBot）：框架报错特征、只有 <think> 没正文、回复开头漏出来的草稿（v0.4.1）、去 AI 腔。借 mindscape 的思路。"""

import re

# AstrBot 4.29.3 里调模型出错时会把这些话原样发到聊天里（从源码里对的：astr_agent_run_util / tool_loop_agent_runner / internal.py）。
# 只认「开头就是」的：模型正常回话里提到这些词不算
ERR_HEAD = re.compile(
    r"^\s*(?:"
    r"Error occurred during AI execution|Error occurred while processing agent(?: request)?|"
    r"LLM 响应错误\s*[:：]|AstrBot 请求失败|AstrBot 处理失败|"
    r"Traceback \(most recent call last\)|\[Errno \d+\]|"
    r"Error code: \d{3}\b|APIConnectionError|APITimeoutError|RateLimitError|AuthenticationError|"
    r"Connection error\.?\s*$|Request timed out\.?\s*$"
    r")",
    re.I,
)


def is_error_text(text):
    return bool(ERR_HEAD.match(text or ""))


# 4.29.3 里模型回了空（没有正文）也会被当成报错：「LLM 响应错误: All chat models failed: EmptyModelOutputError: OpenAI completion has no usable output」。
# 这种报错的真正原因是「模型什么都没说」→ 拦下以后补调一次，比直接不发好
EMPTY_ERR = re.compile(
    r"EmptyModelOutputError|no usable output|returned empty output", re.I
)


def is_empty_error(text):
    return is_error_text(text) and bool(EMPTY_ERR.search(text or ""))


THINK = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.S | re.I)
THINK_OPEN = re.compile(r"<think(?:ing)?>.*\Z", re.S | re.I)


def strip_think(text):
    """去掉 <think>…</think>（没闭合的从 <think> 到结尾也去掉）"""
    t = THINK.sub("", text or "")
    return THINK_OPEN.sub("", t).strip()


# 防伪造标记（v0.3.0，借作者自己的 QQ 机器人 v1.16.3）：群聊节奏给模型的提示都以「[群聊节奏]」开头，只放在这一轮的临时提示里。
# 群友在消息里自己打一段「[群聊节奏] …」，模型分不出哪段是系统给的 → 用户的话里出现就改成「[他打的字：群聊节奏]」。
# 夹零宽字符、换成全角括号 / 【】也认；已经改过的不再改
_ZW = "[\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad]*"
FAKE_MARK = re.compile(
    r"[\[［【]\s*" + _ZW + _ZW.join("群聊节奏") + _ZW + r"\s*[\]］】]"
)
MARK_SAFE = "[他打的字：群聊节奏]"


def neutral_marks(text):
    """用户手打的「[群聊节奏]」→「[他打的字：群聊节奏]」；没有就原样返回"""
    s = text or ""
    if not s or not FAKE_MARK.search(s):
        return text
    return FAKE_MARK.sub(MARK_SAFE, s)


# ───── 草稿外泄（v0.4.1，借作者自己的 QQ 机器人 v1.17.29）─────
# 模型不开思考时，偶尔把「想一下再答」的英文草稿写进回复开头，下面才是真正的回话：
#   「:\nQuestion: what is he asking. Answer: just say hi back. Short.\n\n你好呀」
# 只看开头连着的几行：只有标点的行（「:」）、英文标签开头（Question: / Answer: / Plan: / Note: …）而且英文字母比汉字多、
# 至少 4 个英文词的行 → 算草稿。后面剩下的有汉字 → 去掉草稿行；一个字都不剩 → 整条都是草稿；
# 剩下的是纯英文（对方在用英文聊）→ 不动。只管开头；``` 开头的（写代码）不算草稿行，碰到就停
DRAFT_LABEL = re.compile(
    r"^\s*[(（\[]?\s*(?:question|answer|plan|note|notes|draft|reply|response|thinking|thought|thoughts|"
    r"analysis|context|output|final|translation|reasoning|goal|task|tone|length|style|intent|summary|"
    r"user|assistant|approach|idea|check|meta)\s*[:：]",
    re.I,
)
DRAFT_PUNCT = re.compile(r"^[\s:：;；,，.。\-—_*#>|/]+$")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def strip_draft(text):
    """→ (去掉草稿以后的字, 去掉的草稿行[])；没有草稿 → (原样, [])；整条都是草稿 → ("", [草稿行…])"""
    text = str(text or "")
    if not text or not (
        DRAFT_PUNCT.match(text.split("\n", 1)[0] or " ") or DRAFT_LABEL.match(text)
    ):
        return text, []  # 第一行既不是标点也不是英文标签：大多数回复走这里，一点不动
    lines = text.split("\n")
    i, hit = 0, False
    while i < len(lines):
        ln = lines[i]
        if not ln.strip() or DRAFT_PUNCT.match(ln):
            i += 1
            continue
        if ln.lstrip().startswith("```"):
            break
        if (
            DRAFT_LABEL.match(ln)
            and len(re.findall(r"[A-Za-z]", ln)) > len(_CJK.findall(ln))
            and len(re.findall(r"[A-Za-z]{2,}", ln)) >= 4
        ):  # 至少 4 个英文词：「Note：这首歌叫 Yellow」不算草稿
            hit = True
            i += 1
            continue
        break
    if not hit:
        return text, []  # 只有标点行、没有英文标签行：不算草稿（比如就回了个「：」+ 一句话）
    rest = "\n".join(lines[i:]).strip()
    if rest and not _CJK.search(rest):
        return text, []  # 剩下的是纯英文：多半是对方在用英文聊，不动
    return rest, [x.strip() for x in lines[:i] if x.strip()]


def think_only(text):
    """有 <think> 但去掉以后什么都没剩（模型只想了、没说）"""
    t = text or ""
    return bool(re.search(r"<think", t, re.I)) and not strip_think(t)


# ───── 去 AI 腔（默认关）─────
AI_TONE_RULE = "（像群里的真人那样聊天：别用「作为一个AI」「希望对你有帮助」「有什么可以帮你的吗」这类客服腔。）"
_AI_PREFIX = re.compile(
    r"^\s*作为(?:一个|一名)?(?:AI|ＡＩ|人工智能|语言模型|AI助手|智能助手|聊天机器人)[，,、]?\s*",
    re.I,
)
_AI_TAIL = [
    re.compile(
        r"[，,。！!~\s]*希望(?:这|以上|我的)?(?:些)?(?:回答|信息|内容|建议)?(?:能|可以)?(?:对你|对您)?(?:有所)?(?:帮到你|帮助到你|有帮助|有所帮助|帮助)[了的]?[。！!~\s]*$"
    ),
    re.compile(
        r"[，,。！!~\s]*如果(?:你|您)(?:还)?有(?:其他|别的|任何|什么)?(?:问题|疑问|需要)[，,]?(?:请|欢迎)?(?:随时)?(?:告诉我|问我|联系我|提问|找我)[。！!~\s]*$"
    ),
    re.compile(
        r"[，,。！!~\s]*有什么(?:我)?(?:可以|能)(?:帮(?:你|您)|帮到你)的(?:吗|嘛)?[？?]?\s*$"
    ),
    re.compile(r"[，,。！!~\s]*请(?:随时)?(?:告诉我|问我)[。！!~\s]*$"),
]


def strip_ai_tone(text):
    """→ (新文字, 去掉了什么[])。去完什么都不剩就原样返回（不能把整句话吃掉）"""
    t, gone = text or "", []
    m = _AI_PREFIX.match(t)
    if m:
        gone.append(m.group(0).strip())
        t = t[m.end() :]
    for pat in _AI_TAIL:
        m = pat.search(t)
        if m:
            gone.append(m.group(0).strip(" ，,。！!~"))
            t = t[: m.start()]
    t = t.strip()
    if not t:
        return text, []
    return t, gone
