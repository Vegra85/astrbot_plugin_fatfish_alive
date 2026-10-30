#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端测试（活人感版：原 群聊节奏 插件的 62 项；配置在 rhythm 分组、存档在 state.json 的 rhythm 块、面板 action 带 m=rhythm。原来那个假的省 token 插件换成活人感自己的省 token 模块：「省 token 说算了」用把意愿压到 0 来做，「用量记没记」改成对账——假模型叫了几次、省 token 记了几次回话）：真的 AstrBot（4.29.3）+ 这个插件 + 假 QQ 端（假 NapCat 反向 WebSocket）+ 假模型（OpenAI 兼容，按规则回话）
另外装两个测试用的小插件：
  「kindprobe」：在 AstrBot 叫模型之前记下 event 上的 fatfish_kind（省 token 插件看到的就是这个）；还带一个指令 /e2eping（模拟别的插件发的话）
  假的省 token 插件（名字跟真的一样，只有 ask / record_usage 两个公开方法）：看主动说话前问没问、自己调模型的用量记没记
AstrBot 开着「群聊消息记录注入上下文」（README 建议开的）
第一段（挑着接的概率 = 1、最多连着接 2 轮）：
  A 接话：@ 它以后同一个人不带 @ 说「你刚才说的啥意思」→ 回、fatfish_kind = follow、走的是 AstrBot 正常流程（带对话记录、人格）
  B 在线时别人随便聊 → 挑着接（chat）；模型回 [[不说]] → 不发、不进对话记录，但那一轮的话和当时的群聊上下文补进下一轮
  C 别人 @ 别人 / 引用别人 → 不抢话
  D 同一个人不 @ 连着接到 2 轮 → 停；@ 一下重新算
  E 分数在中间 → 让模型判断（假模型回「是」→ 接；回「不是」→ 不接）
  F 别人家的 AI（在认 AI 插件的 state.json 里）→ 不接
  G 别的插件发的话（/e2eping 的回复）也记下：有人接着它说 → 接话；「会话管理」里关掉本插件的群：不接、不进在线
  H 面板接口：状态、群的状态、今天的数、最近的判断；没登录读不到；面板页认得出；面板里让一个群回观望
第二段（WebUI 改配置 → 插件热重载；挑着接的概率 = 0、只在 3 个群里用、判断超时 3 秒）：别人随便聊 → 不接；接话照样接；
  今天的数重载后还在；没列的群不接话；模型判断太慢 → 算不接，等的时候别的群照常回
第三段（时间参数调小）：省 token 插件说「算了」→ 不试探；冷场试探发得出去（带人格、带最近的群聊）、到点收尾发得出去、
  收尾后冷却期不接话但被 @ 照样回、主动说的话记进了 AstrBot 的对话记录
用法：python tests/e2e_astrbot.py [插件目录 默认上一层] [--keep]
  需要装了 astrbot（测过 4.29.3）的 Python，astrbot 命令在 PATH 里（或者跟这个 Python 在同一个 bin 目录）；另外要 aiohttp、pyjwt、websockets
  只在临时文件夹里跑：模型是假的、QQ 端是假的，不连外网
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from aiohttp import web
import jwt
import websockets

PLUGIN_SRC = Path(
    sys.argv[1]
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--")
    else Path(__file__).resolve().parent.parent
).resolve()
KEEP = "--keep" in sys.argv
PNAME = PLUGIN_SRC.name
SELF, A, B, C, AI = 99999, 30001, 30002, 30003, 50005
G1, G2, G3, G4, G5, G6, G7 = 7001, 7002, 7003, 7004, 7005, 7006, 7007
RES = []
BIN = Path(sys.executable).parent
ASTRBOT = str(BIN / "astrbot") if (BIN / "astrbot").exists() else "astrbot"


def check(cond, msg, why=""):
    RES.append((bool(cond), msg))
    print(
        ("  [过]   " if cond else "  [没过] ")
        + msg
        + ("" if cond or not why else f"\n           {str(why)[:900]}"),
        flush=True,
    )
    return bool(cond)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


LLM_PORT, WS_PORT, DASH_PORT = free_port(), free_port(), free_port()
ROOT = Path(tempfile.mkdtemp(prefix="astrbot-e2e-rhythm-"))
KIND_LOG = ROOT / "kinds.jsonl"
SECRET = "e2e-secret-0123456789abcdef0123456789"
chat_calls, judge_calls, line_calls = [], [], []
JUDGE = {"answer": "不是", "delay": 0.0}
PERSONA = "You are a helpful and friendly assistant."  # AstrBot 自带的默认人格


def text_of(m):
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(
            p.get("text", "")
            for p in c
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def first_text(m):
    """用户这一轮的第一块文字 = 他说的那句话（后面的块是插件 / AstrBot 附的提示、群聊上下文）"""
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return next(
            (
                p.get("text", "")
                for p in c
                if isinstance(p, dict) and p.get("type") == "text"
            ),
            "",
        )
    return ""


# ───────────── 假模型 ─────────────
async def chat_completions(request):
    body = await request.json()
    msgs = body.get("messages") or []
    system = "\n".join(text_of(m) for m in msgs if m.get("role") == "system")
    last = text_of(msgs[-1]) if msgs else ""
    if "你在旁观一个 QQ 群" in system:
        judge_calls.append({"system": system, "body": last, "t": time.time()})
        if JUDGE["delay"]:
            await asyncio.sleep(JUDGE["delay"])
        text = JUDGE["answer"]
    elif "突然安静了几分钟" in system:
        line_calls.append(
            {"kind": "probe", "system": system, "prompt": last, "t": time.time()}
        )
        text = "试探：话说回来，火锅蘸料你们都放啥"
    elif "现在想下线" in system:
        line_calls.append(
            {"kind": "exit", "system": system, "prompt": last, "t": time.time()}
        )
        text = "收尾：我先去吃饭了 你们聊"
    else:
        chat_calls.append(
            {"messages": msgs, "system": system, "last": last, "t": time.time()}
        )
        text = (
            "[[不说]]"
            if "SILENT" in (first_text(msgs[-1]) if msgs else "")
            else f"收到第{len(chat_calls)}句"
        )
    return web.json_response(
        {
            "id": "x",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", "fake"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "total_tokens": 110,
            },
        }
    )


async def models(request):
    return web.json_response(
        {"object": "list", "data": [{"id": "fake-model", "object": "model"}]}
    )


# ───────────── 假 QQ 端（NapCat 反向 WebSocket） ─────────────
class FakeQQ:
    def __init__(self):
        self.ws = None
        self.mid = 1000 + int(time.time()) % 1000 * 10
        self.sent = []
        self.store = {}
        self.names = {
            SELF: "胖头鱼",
            A: "老王",
            B: "小李",
            C: "阿珍",
            AI: "隔壁的机器人",
        }
        self.new_sent = asyncio.Event()

    async def connect(self):
        for _ in range(240):
            try:
                self.ws = await websockets.connect(
                    f"ws://127.0.0.1:{WS_PORT}/ws/",
                    additional_headers={
                        "X-Self-ID": str(SELF),
                        "X-Client-Role": "Universal",
                    },
                )
                break
            except Exception:
                await asyncio.sleep(0.5)
        if not self.ws:
            raise RuntimeError("连不上 AstrBot 的反向 WebSocket")
        asyncio.create_task(self.loop())
        await self.ws.send(
            json.dumps(
                {
                    "post_type": "meta_event",
                    "meta_event_type": "lifecycle",
                    "sub_type": "connect",
                    "self_id": SELF,
                    "time": int(time.time()),
                }
            )
        )

    async def loop(self):
        try:
            async for raw in self.ws:
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                if "action" not in d:
                    continue
                act, p, echo = d["action"], d.get("params") or {}, d.get("echo")
                data = {}
                if act in ("send_group_msg", "send_msg"):
                    self.mid += 1
                    msg = p.get("message")
                    text = (
                        "".join(
                            (s.get("data") or {}).get("text", "")
                            for s in msg
                            if isinstance(s, dict) and s.get("type") == "text"
                        )
                        if isinstance(msg, list)
                        else str(msg)
                    )
                    gid = int(p.get("group_id") or 0)
                    self.sent.append((gid, text, time.time(), self.mid))
                    self.store[self.mid] = {
                        "post_type": "message",
                        "message_type": "group",
                        "sub_type": "normal",
                        "message_id": self.mid,
                        "group_id": gid,
                        "user_id": SELF,
                        "self_id": SELF,
                        "time": int(time.time()),
                        "message": msg if isinstance(msg, list) else [],
                        "raw_message": text,
                        "font": 0,
                        "sender": {
                            "user_id": SELF,
                            "nickname": "胖头鱼",
                            "card": "",
                            "role": "member",
                        },
                    }
                    data = {"message_id": self.mid}
                    self.new_sent.set()
                elif act == "get_group_member_info":
                    uid = int(p.get("user_id") or 0)
                    data = {
                        "user_id": uid,
                        "nickname": self.names.get(uid, str(uid)),
                        "card": self.names.get(uid, ""),
                        "role": "member",
                    }
                elif act == "get_stranger_info":
                    uid = int(p.get("user_id") or 0)
                    data = {"user_id": uid, "nickname": self.names.get(uid, str(uid))}
                elif act == "get_msg":
                    data = self.store.get(int(p.get("message_id") or 0)) or {}
                elif act == "get_login_info":
                    data = {"user_id": SELF, "nickname": "胖头鱼"}
                await self.ws.send(
                    json.dumps(
                        {"status": "ok", "retcode": 0, "data": data, "echo": echo}
                    )
                )
        except Exception:
            pass

    async def say(self, gid, uid, text="", at_bot=False, at=None, quote=None):
        self.mid += 1
        segs = []
        if quote:
            segs.append({"type": "reply", "data": {"id": str(quote)}})
        if at_bot:
            segs.append({"type": "at", "data": {"qq": str(SELF)}})
        if at:
            segs.append({"type": "at", "data": {"qq": str(at)}})
        if text:
            segs.append(
                {"type": "text", "data": {"text": (" " if at_bot or at else "") + text}}
            )
        ev = {
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "message_id": self.mid,
            "group_id": gid,
            "user_id": uid,
            "self_id": SELF,
            "time": int(time.time()),
            "message": segs,
            "raw_message": text,
            "font": 0,
            "sender": {
                "user_id": uid,
                "nickname": self.names.get(uid, str(uid)),
                "card": self.names.get(uid, ""),
                "role": "member",
            },
        }
        self.store[self.mid] = dict(ev)
        await self.ws.send(json.dumps(ev, ensure_ascii=False))
        return self.mid

    async def wait_sent(self, gid, since, timeout=15.0, has=""):
        end = time.time() + timeout
        while time.time() < end:
            got = [
                s for s in self.sent if s[0] == gid and s[2] >= since and has in s[1]
            ]
            if got:
                return got
            self.new_sent.clear()
            try:
                await asyncio.wait_for(
                    self.new_sent.wait(), max(0.05, end - time.time())
                )
            except asyncio.TimeoutError:
                pass
        return []


# ───────────── 测试用的小插件：记下 fatfish_kind；/e2eping 模拟别的插件发的话 ─────────────
PROBE_META = """name: astrbot_plugin_e2e_kindprobe
desc: e2e 测试用：记下叫模型之前 event 上的 fatfish_kind
version: v0.0.1
author: e2e
"""
PROBE_MAIN = """# -*- coding: utf-8 -*-
import json
import os
import time

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


class KindProbe(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)

    @filter.on_waiting_llm_request(priority=0)
    async def probe(self, event: AstrMessageEvent):
        with open(os.environ["KIND_LOG"], "a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": event.get_extra("fatfish_kind"), "text": event.message_str,
                                "uid": str(event.get_sender_id()), "g": str(event.get_group_id()), "t": time.time()},
                               ensure_ascii=False) + "\\n")
        if "MERGED" in (event.message_str or ""):        # 模拟「攒一攒」把前面直接 @ 它的那条并了进来：清掉 fatfish_kind
            event.set_extra("fatfish_kind", None)
            event.is_at_or_wake_command = True

    @filter.command("e2eping")
    async def ping(self, event: AstrMessageEvent):
        yield event.plain_result("pong：这是别的插件发的话")
"""


def kinds():
    try:
        return [
            json.loads(x)
            for x in KIND_LOG.read_text(encoding="utf-8").splitlines()
            if x.strip()
        ]
    except FileNotFoundError:
        return []


def plugin_defaults():
    schema = json.loads((PLUGIN_SRC / "_conf_schema.json").read_text(encoding="utf-8"))
    return {
        g: {k: v.get("default") for k, v in (gv.get("items") or {}).items()}
        for g, gv in schema.items()
    }


def conf(rhythm=None, budget=None):
    """整份配置：别的模块这套测试不测（关掉，条件才跟原来单独装群聊节奏一样）；省 token 开着、靠「火力全开」让它不掷骰子"""
    c = plugin_defaults()
    c["gather"]["enable"] = False
    c["guard"]["enable"] = False
    c["rhythm"].update(rhythm or {})
    c["budget"].update(budget or {})
    return c


DENY = dict(
    floor=0.0, half_usd=1e-9, power_private=0.0, power_direct=0.0
)  # 把回复意愿压到 0：省 token 说「算了」


PHASE1 = dict(
    topic_overlap=False,
    reply_prob=1.0,
    min_gap_sec=0,
    max_turns=2,
    online_min=60.0,
    online_max=60.0,
    idle_min=60.0,
    max_minutes=120.0,
)
PHASE2 = dict(
    PHASE1, reply_prob=0.0, judge_timeout_sec=3.0, groups=[str(G1), str(G2), str(G4)]
)
PHASE3 = dict(
    topic_overlap=False,
    reply_prob=0.0,
    min_gap_sec=0,
    online_min=0.4,
    online_max=0.4,
    max_minutes=0.5,
    idle_min=0.08,
    probe_prob=1.0,
    probe_wait_min=0.5,
    exit_line_prob=1.0,
    exit_if_chat_within_min=1.0,
    cooldown_min=1.0,
    lines_per_hour=10,
)


def setup_root():
    env = dict(os.environ, ASTRBOT_ROOT=str(ROOT))
    subprocess.run(
        [ASTRBOT, "init", "-y"], cwd=ROOT, env=env, check=True, capture_output=True
    )
    shutil.copytree(
        PLUGIN_SRC,
        ROOT / "data" / "plugins" / PNAME,
        ignore=shutil.ignore_patterns("__pycache__", "dev", "*.pyc", ".git", "tests"),
    )
    probe = ROOT / "data" / "plugins" / "astrbot_plugin_e2e_kindprobe"
    probe.mkdir(parents=True)
    (probe / "metadata.yaml").write_text(PROBE_META, encoding="utf-8")
    (probe / "main.py").write_text(PROBE_MAIN, encoding="utf-8")
    code = (
        "from astrbot.core.config.astrbot_config import AstrBotConfig;"
        f"AstrBotConfig(config_path=r'{ROOT / 'data' / 'cmd_config.json'}')"
    )
    subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, check=True, capture_output=True
    )
    p = ROOT / "data" / "cmd_config.json"
    cfg = json.loads(p.read_text(encoding="utf-8-sig"))
    cfg["platform"] = [
        {
            "id": "napcat",
            "type": "aiocqhttp",
            "enable": True,
            "ws_reverse_host": "127.0.0.1",
            "ws_reverse_port": WS_PORT,
            "ws_reverse_token": "",
        }
    ]
    cfg["platform_settings"]["rate_limit"]["count"] = 1000
    cfg["provider_ltm_settings"]["group_icl_enable"] = (
        True  # README 建议开着的「群聊消息记录注入上下文」
    )
    cfg.setdefault("dashboard", {})["port"] = DASH_PORT
    cfg["dashboard"]["host"] = "127.0.0.1"
    cfg["dashboard"]["jwt_secret"] = SECRET
    base = {
        "type": "openai_chat_completion",
        "provider_type": "chat_completion",
        "enable": True,
        "key": ["sk-fake"],
        "api_base": f"http://127.0.0.1:{LLM_PORT}/v1",
        "timeout": 30,
    }
    cfg["provider"] = [
        {"id": "fake", **base, "model": "fake-model", "modalities": ["text"]}
    ]
    cfg.setdefault("provider_settings", {})["default_provider_id"] = "fake"
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    pc = ROOT / "data" / "config" / f"{PNAME}_config.json"
    pc.parent.mkdir(parents=True, exist_ok=True)
    pc.write_text(
        json.dumps(conf(PHASE1), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 认 AI 插件存的名单（这次没装那个插件：这个插件就读它的 state.json）
    ai = ROOT / "data" / "plugin_data" / "astrbot_plugin_fatfish_aiguard"
    ai.mkdir(parents=True, exist_ok=True)
    (ai / "state.json").write_text(
        json.dumps(
            {
                "bots": {
                    str(AI): {
                        "name": "隔壁的机器人",
                        "why": "测试",
                        "t": int(time.time()),
                    }
                },
                "humans": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def log_text():
    return (ROOT / "astrbot_stdout.log").read_text(encoding="utf-8", errors="replace")


async def turn(qq, gid, uid, text="", expect=True, quiet=3.0, **kw):
    """说一句 → 等机器人回（expect=False 时等 quiet 秒确认没回）→ 返回 (回了没有, 这一轮的回话请求, 这一轮记下的 kind)"""
    nc, nk, t0 = len(chat_calls), len(kinds()), time.time()
    await qq.say(gid, uid, text, **kw)
    got = await qq.wait_sent(gid, t0, timeout=15 if expect else quiet)
    await asyncio.sleep(
        0.8 if got else 0.3
    )  # 让 after_message_sent 记完（真人也不会一秒内接话）
    chat = chat_calls[nc] if len(chat_calls) > nc else None
    ks = kinds()[nk:]
    return bool(got), chat, ks


def hist_text(chat):
    return "\n".join(text_of(m) for m in (chat["messages"][:-1] if chat else []))


async def api_get(s, hdr, path):
    async with s.get(
        f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/extensions/{PNAME}/{path}",
        headers=hdr,
    ) as r:
        d = await r.json(content_type=None)
        if (
            path == "state"
            and isinstance((d or {}).get("data"), dict)
            and "rhythm" in d["data"]
        ):
            d = dict(
                d, data=d["data"]["rhythm"], whole=d["data"]
            )  # 其余用例都看 rhythm 这一块；整份留在 whole 里
        return r.status, d


async def api_post(s, hdr, body):
    async with s.post(
        f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/extensions/{PNAME}/action",
        headers=hdr,
        json=body,
    ) as r:
        return r.status, await r.json(content_type=None)


async def budget_of(s, hdr):
    return (((await api_get(s, hdr, "state"))[1]).get("whole") or {}).get(
        "budget"
    ) or {}


def llm_total():
    return len(chat_calls) + len(judge_calls) + len(line_calls)


async def set_config(s, hdr, over, budget=None):
    cfg = conf(over, budget)
    async with s.put(
        f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/{PNAME}/config",
        headers=hdr,
        json={"config": cfg},
    ) as r:
        d = await r.json(content_type=None)
    await asyncio.sleep(1.5)
    return r.status, d


async def main():
    setup_root()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat_completions)
    app.router.add_get("/v1/models", models)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", LLM_PORT).start()

    env = dict(
        os.environ,
        ASTRBOT_ROOT=str(ROOT),
        DASHBOARD_PORT=str(DASH_PORT),
        PYTHONUNBUFFERED="1",
        KIND_LOG=str(KIND_LOG),
    )
    log = open(ROOT / "astrbot_stdout.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [ASTRBOT, "run"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT
    )
    token = jwt.encode(
        {"username": "astrbot", "exp": int(time.time()) + 3600},
        SECRET,
        algorithm="HS256",
    )
    hdr = {"Authorization": f"Bearer {token}"}
    try:
        qq = FakeQQ()
        await qq.connect()
        for _ in range(60):  # 等插件都加载完（机器慢的时候要十几秒）
            if all(x in log_text() for x in (PNAME, "astrbot_plugin_e2e_kindprobe")):
                break
            await asyncio.sleep(0.5)
        await asyncio.sleep(3)
        text = log_text()
        check(
            PNAME in text
            and "[活人感] v0.1.0 已启用" in text
            and "Traceback" not in text,
            "AstrBot 起来了、插件加载了、没有报错",
            text[-1500:],
        )
        s = aiohttp.ClientSession()
        code, d = await api_post(s, hdr, {"m": "budget", "do": "full", "hours": -1})
        check(
            code == 200 and (d or {}).get("status") == "ok",
            "省 token 开着，先「火力全开」一直开（前面的用例不掷骰子，才跟原来那个测试的条件一样）",
            d,
        )

        # ═════════ A 接话 ═════════
        print("\n[A 接话：@ 它以后同一个人不带 @ 接着说]")
        ok, chat_at, ks = await turn(qq, G1, A, "大家好呀", at_bot=True)
        check(
            ok and ks and ks[-1]["kind"] is None,
            "@ 它：回了（AstrBot 本来就回，不标 fatfish_kind）",
            ks,
        )
        ok, chat, ks = await turn(qq, G1, A, "你刚才说的啥意思")
        check(ok, "同一个人不带 @ 说「你刚才说的啥意思」：回了")
        check(
            ks and ks[-1]["kind"] == "follow" and ks[-1]["text"] == "你刚才说的啥意思",
            "叫模型之前 event 上 fatfish_kind = follow（省 token 插件看到的）",
            ks,
        )
        lu = chat["last"] if chat else ""
        check(
            "你刚才说的啥意思" in lu
            and "[群聊节奏] 这句没 @ 你" in lu
            and "说的是「你」" in lu
            and "[[不说]]" in lu,
            "回话请求里：这条消息 + 这一轮的提示（为什么当成在接它的话）",
            lu[-600:],
        )
        check(
            "收到第1句" in hist_text(chat) and "大家好呀" in hist_text(chat),
            "走的是 AstrBot 正常流程：带着这个群的对话记录",
            hist_text(chat)[-400:],
        )
        check(
            chat
            and chat_at
            and PERSONA in chat["system"]
            and chat["system"].split("\n")[0] == chat_at["system"].split("\n")[0],
            "带着人格（跟 @ 它时的 system prompt 一样开头）",
            chat and chat["system"][:300],
        )

        # ═════════ B 在线时挑着接 ═════════
        print("\n[B 在线时别人随便聊（挑着接的概率 = 1）]")
        ok, chat, ks = await turn(qq, G1, B, "今天食堂的饭真难吃")
        check(
            ok and ks and ks[-1]["kind"] == "chat",
            "没点名的一句：挑着接了，fatfish_kind = chat",
            ks,
        )
        check(
            chat and "[群聊节奏] 你正在群里跟大家聊着" in chat["last"],
            "回话请求里有「你正在群里跟大家聊着」",
            chat and chat["last"][-300:],
        )
        check(
            "[群聊节奏]" not in hist_text(chat),
            "上一轮的提示没进对话记录（只给那一轮）",
            hist_text(chat)[-500:],
        )
        nsent = len(qq.sent)
        await qq.say(
            G1, B, "晚上食堂有红烧肉", at=C
        )  # 在跟阿珍说：不抢话；AstrBot 的群聊上下文记下了
        await asyncio.sleep(1.5)
        ok, chat, ks = await turn(qq, G1, C, "SILENT 随便说说", expect=False)
        check(
            not ok
            and chat is not None
            and len(qq.sent) == nsent
            and ks
            and ks[-1]["kind"] == "chat",
            "模型回 [[不说]]：问了模型，但什么都没发",
            (ok, chat is not None, ks),
        )
        check(
            chat is not None and "晚上食堂有红烧肉" in chat["last"],
            "那一轮带着 AstrBot 注入的群聊上下文（「群聊消息记录注入上下文」开着）",
            chat and chat["last"][-400:],
        )
        ok, chat, ks = await turn(qq, G1, A, "那晚上吃啥", at_bot=True)
        check(
            ok
            and "[[不说]]" not in hist_text(chat)
            and "SILENT" not in hist_text(chat),
            "[[不说]] 那一轮没进对话记录",
            hist_text(chat)[-500:],
        )
        lu = chat["last"] if chat else ""
        check(
            "SILENT 随便说说" in lu
            and "晚上食堂有红烧肉" in lu
            and "你上次没接话" in lu,
            "没接话那一轮的话 + 当时的群聊上下文：补进了下一次叫模型（AstrBot 注入一次就用掉了，不补就丢了）",
            lu[-700:],
        )

        # ═════════ C 不抢话 ═════════
        print("\n[C 别人在 @ 别人 / 引用别人]")
        mid_b = await qq.say(G1, B, "我觉得还行")
        await asyncio.sleep(2.5)
        ok, chat, ks = await turn(qq, G1, C, "你说得对吗", at=B, expect=False)
        check(
            not ok and not ks, "@ 别人（哪怕说了「你」、挑着接的概率 = 1）：不抢话", ks
        )
        ok, chat, ks = await turn(qq, G1, C, "哈哈哈", quote=mid_b, expect=False)
        check(not ok and not ks, "引用别人的话：不抢话", ks)

        # ═════════ D 最多连着接几轮 ═════════
        print("\n[D 同一个人不 @ 最多连着接 2 轮]")
        ok, _, _ = await turn(qq, G2, A, "在吗", at_bot=True)
        ok1, _, k1 = await turn(qq, G2, A, "你是谁呀")
        ok2, _, k2 = await turn(qq, G2, A, "你喜欢吃什么")
        check(
            ok and ok1 and ok2 and [x["kind"] for x in k1 + k2] == ["follow", "follow"],
            "接了 2 轮",
            (ok, ok1, ok2, k1, k2),
        )
        ok3, _, k3 = await turn(qq, G2, A, "你怎么不理我呀", expect=False)
        check(
            not ok3 and not k3,
            "第 3 轮：不接了（挑着接也不接，防止两个机器人聊个没完）",
            k3,
        )
        ok, _, _ = await turn(qq, G2, A, "喂", at_bot=True)
        ok4, _, k4 = await turn(qq, G2, A, "你还在吗")
        check(
            ok and ok4 and k4 and k4[-1]["kind"] == "follow",
            "@ 一下：重新算，又接了",
            k4,
        )

        # ═════════ E 让模型判断 ═════════
        print("\n[E 分数在中间：让模型判断]")
        JUDGE["answer"] = "是"
        nj = len(judge_calls)
        ok, _, ks = await turn(qq, G2, C, "这个怎么弄")
        jc = judge_calls[nj] if len(judge_calls) > nj else {}
        check(
            ok and ks and ks[-1]["kind"] == "follow" and len(judge_calls) == nj + 1,
            "模型说「是」：接了（follow）",
            (ok, ks),
        )
        check(
            "「胖头鱼」（是个 AI）" in jc.get("system", "")
            and "[刚刚] 阿珍: 这个怎么弄   ← 判断这一条" in jc.get("body", "")
            and "胖头鱼: 收到第" in jc.get("body", ""),
            "给模型看的：最近几条（机器人叫 QQ 昵称）+ 要判断的这条",
            jc,
        )
        JUDGE["answer"] = "不是"
        ok, _, ks = await turn(qq, G2, B, "这个怎么弄啊", expect=False)
        check(
            not ok and not ks and len(judge_calls) == nj + 2,
            "模型说「不是」：不接（也不挑着接）",
            (ok, ks, len(judge_calls) - nj),
        )
        await asyncio.sleep(1.0)
        bs = await budget_of(s, hdr)
        calls_rec = (bs.get("parts") or {}).get("calls", 0)
        check(
            calls_rec == llm_total()
            and (bs.get("parts") or {}).get("out", {}).get("tok") == 10 * llm_total(),
            "对账：假模型总共被叫了几次（回话 + 判断），省 token 就记了几次（让模型判断用掉的也记了），输出 token 也对得上",
            (calls_rec, llm_total(), bs.get("parts")),
        )

        # ═════════ F 别人家的 AI ═════════
        print("\n[F 别人家的 AI（认 AI 插件的名单里）]")
        ok, _, ks = await turn(qq, G2, AI, "你说的不对吧？", expect=False)
        check(
            not ok and not ks and "是别人家的 AI" in log_text(),
            "没 @：不接话、不挑着接（读的认 AI 插件的 state.json）",
            ks,
        )

        # ═════════ G 别的插件发的话 ═════════
        print("\n[G 别的插件发的话也记下]")
        ok, _, ks = await turn(qq, G4, A, "/e2eping")
        check(
            ok and not ks and any("pong" in x[1] for x in qq.sent if x[0] == G4),
            "/e2eping：别的插件回了（没叫模型）",
            ks,
        )
        ok, chat, ks = await turn(qq, G4, A, "你这个pong是啥")
        check(
            ok and ks and ks[-1]["kind"] == "follow",
            "接着那个插件的话说：当成在接它的话（after_message_sent 记下了它说的）",
            ks,
        )
        ok, chat, ks = await turn(qq, G4, A, "MERGED 你刚才那句再说一遍")
        check(
            ok
            and ks
            and ks[-1]["kind"] == "follow"
            and chat
            and "[群聊节奏]" not in chat["last"],
            "别的插件把 fatfish_kind 清掉了（像攒一攒并进了直接 @ 它的那条）：不再附「这句没 @ 你」",
            chat and chat["last"][-300:],
        )

        # ═════════ 会话管理里关掉本插件 ═════════
        print("\n[AstrBot「会话管理」里给一个群关掉本插件]")
        rule = {
            "umo": f"napcat:GroupMessage:{G6}",
            "rule_key": "session_plugin_config",
            "rule_value": {"enabled_plugins": [], "disabled_plugins": [PNAME]},
        }
        async with s.post(
            f"http://127.0.0.1:{DASH_PORT}/api/session/update-rule",
            headers=hdr,
            json=rule,
        ) as r:
            d = await r.json(content_type=None)
        check(r.status == 200 and (d or {}).get("status") == "ok", "关掉了", d)
        ok, _, _ = await turn(qq, G6, A, "在吗", at_bot=True)
        ok2, _, ks2 = await turn(qq, G6, A, "你刚才说的啥意思", expect=False)
        st_code, d = await api_get(s, hdr, "state")
        g6 = {g["g"]: g for g in ((d or {}).get("data") or {}).get("groups") or []}.get(
            str(G6), {}
        )
        check(
            ok and not ok2 and not ks2 and g6.get("mode") != "在线",
            "被 @ 照常回（AstrBot 自己回的）；不带 @ 接着说不接；也没进在线（不会去那个群试探、收尾）",
            (ok, ok2, ks2, g6),
        )

        # ═════════ H 面板 ═════════
        print("\n[H 面板接口]")
        st_code, d = await api_get(s, hdr, "state")
        st = (d or {}).get("data") or {}
        today = st.get("today") or {}
        gm = {g["g"]: g for g in st.get("groups") or []}
        check(
            st_code == 200
            and today.get("follow", 0) >= 6
            and today.get("chat", 0) >= 1
            and today.get("silent", 0) >= 1
            and today.get("judge", 0) >= 2
            and today.get("judge_yes", 0) >= 1
            and today.get("skip_ai", 0) >= 1,
            f"今天的数：接话 {today.get('follow')}、挑着接 {today.get('chat')}、没说 {today.get('silent')}、"
            f"让模型判断 {today.get('judge')}（说是 {today.get('judge_yes')}）、别人家的 AI {today.get('skip_ai')}",
            today,
        )
        check(
            gm.get(str(G1), {}).get("mode") == "在线"
            and gm.get(str(G1), {}).get("left_sec", 0) > 3000,
            "群的状态：G1 在线、还剩快 60 分钟",
            gm.get(str(G1)),
        )
        dec = st.get("decisions") or []
        check(
            any(x["verdict"] == "model_yes" and x["took"] for x in dec)
            and any(x["verdict"] == "model_no" for x in dec)
            and any("连着接了 2 轮" in x["why"] for x in dec)
            and any(x["verdict"] == "yes" and x["score"] >= 4 for x in dec),
            "最近的判断：分够了接、模型说是 / 不是、轮数到了",
            dec[:6],
        )
        async with s.get(
            f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/extensions/{PNAME}/state"
        ) as r:
            check(r.status in (401, 403), f"没登录读不到（{r.status}）")
        code_l, d = await api_post(
            s, hdr, {"m": "rhythm", "op": "lurk", "gk": f"napcat:{G2}"}
        )
        gm2 = {g["g"]: g for g in ((d or {}).get("data") or {}).get("groups") or []}
        check(
            code_l == 200 and gm2.get(str(G2), {}).get("mode") == "观望",
            "面板里让 G2 回观望",
            d,
        )
        async with s.get(
            f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/{PNAME}/views/panel",
            headers=hdr,
        ) as r:
            d = await r.json(content_type=None)
        url = None

        def _find(o):
            nonlocal url
            if isinstance(o, dict):
                for v in o.values():
                    if isinstance(v, str) and "/_t/" in v and url is None:
                        url = v
                    _find(v)
            elif isinstance(o, list):
                for v in o:
                    _find(v)

        _find(d)
        check(
            r.status == 200 and url,
            "面板页 panel 认出来了",
            json.dumps(d, ensure_ascii=False)[:400],
        )
        if url:
            full = (
                url if url.startswith("http") else f"http://127.0.0.1:{DASH_PORT}{url}"
            )
            async with s.get(full, headers=hdr) as r2:
                html = await r2.text()
            check(
                r2.status == 200 and "群聊节奏" in html and "bridge-sdk.js" in html,
                "面板页 HTML 拿得到、AstrBot 插上了桥接脚本",
                (r2.status, html[:200]),
            )
        # ═════════ 有人 @ 它说「欢迎新人」（v0.1.1）═════════
        print("\n[有人 @ 它说「欢迎新人」：是在欢迎它（v0.1.1）]")
        G8 = 7008
        ok, chat, ks = await turn(qq, G8, C, "欢迎新人！", at_bot=True)
        lu = chat["last"] if chat else ""
        check(
            ok
            and "阿珍 这句 @ 了你" in lu
            and "是在欢迎你进群" in lu
            and "不是阿珍" in lu,
            "回话请求里提醒它：这句是在欢迎它（它是新来的那个）",
            lu[-500:],
        )
        ok2, chat2, ks2 = await turn(qq, G8, C, "欢迎来玩呀", at_bot=True)
        ok3, chat3, ks3 = await turn(qq, G8, C, "今天食堂有红烧肉", at_bot=True)
        check(
            ok3
            and "是在欢迎你进群" not in (chat3["last"] if chat3 else "")
            and "是在欢迎你进群" not in hist_text(chat3),
            "不是欢迎的话不加；上一轮的欢迎提示也没进对话记录",
            (chat3 or {}).get("last", "")[-300:],
        )
        check("@ 它说欢迎 → 提醒它是在欢迎它自己" in log_text(), "日志里记了一笔")

        text = log_text()
        check(
            "Traceback" not in text,
            "第一段 AstrBot 日志里没有 Traceback",
            text[text.find("Traceback") - 300 :][:1500],
        )

        # ═════════ 第二段：挑着接的概率 = 0 ═════════
        print("\n[第二段：WebUI 改配置（插件热重载），挑着接的概率 = 0]")
        code, d = await set_config(s, hdr, PHASE2)
        check(
            code == 200 and (d or {}).get("status") == "ok",
            "改配置成功（插件热重载）",
            d,
        )
        st_code, d = await api_get(s, hdr, "state")
        t2 = ((d or {}).get("data") or {}).get("today") or {}
        check(
            t2.get("follow") == today.get("follow")
            and t2.get("chat") == today.get("chat"),
            "今天的数重载以后还在（存在 state.json 里）",
            t2,
        )
        ok, _, ks = await turn(qq, G1, A, "我回来啦", at_bot=True)
        ok2, _, ks2 = await turn(qq, G1, B, "今天食堂的饭真难吃", expect=False)
        check(ok and not ok2 and not ks2, "在线时别人随便聊：概率 0 → 不接", ks2)
        ok3, _, ks3 = await turn(qq, G1, A, "你刚才说的啥意思")
        check(ok3 and ks3 and ks3[-1]["kind"] == "follow", "接话照样接", ks3)
        ok, _, _ = await turn(qq, G7, A, "在吗", at_bot=True)
        ok2, _, ks2 = await turn(qq, G7, A, "你刚才说的啥意思", expect=False)
        check(
            ok and not ok2 and not ks2,
            "「只在这些群里用」没列的群：被 @ 照常回，不带 @ 接着说不接",
            (ok, ok2, ks2),
        )

        print("\n[第二段：让模型判断的时候模型很慢（假模型 8 秒才答，超时设 3 秒）]")
        JUDGE["answer"], JUDGE["delay"] = "是", 8.0
        ok, _, _ = await turn(qq, G2, A, "我回来了", at_bot=True)
        nj, t_ask = len(judge_calls), time.time()
        await qq.say(G2, C, "这个怎么弄")  # 2.5 分 → 要问模型
        await asyncio.sleep(0.3)
        await qq.say(G4, B, "在吗", at_bot=True)
        other = await qq.wait_sent(G4, t_ask, timeout=10.0)
        waited = (other[0][2] - t_ask) if other else 99.0
        check(
            ok and other and waited < 3.0 and len(judge_calls) == nj + 1,
            f"等模型判断的那几秒里，别的群照常回（{round(waited, 1)} 秒就回了，没被卡住）",
            (ok, waited, len(judge_calls) - nj),
        )
        got = await qq.wait_sent(G2, t_ask, timeout=7.0)
        st_code, d = await api_get(s, hdr, "state")
        dec = ((d or {}).get("data") or {}).get("decisions") or []
        check(
            not got
            and dec
            and dec[0]["verdict"] == "model_fail"
            and "3 秒没答" in dec[0]["why"]
            and not dec[0]["took"],
            "模型超过 3 秒没答：算不是，不接（面板记下了为什么）",
            (got, dec[:1]),
        )
        JUDGE["answer"], JUDGE["delay"] = "不是", 0.0

        # ═════════ 第三段：冷场试探 / 收尾 ═════════
        print("\n[第三段：时间参数调小，冷场试探 / 到点收尾 / 收尾冷却]")
        code, d = await set_config(s, hdr, PHASE3)
        check(code == 200, "改配置成功", d)
        await api_post(s, hdr, {"m": "budget", "do": "full", "hours": 0})  # 关火力全开
        code, d = await set_config(
            s, hdr, PHASE3, DENY
        )  # 把回复意愿压到 0：省 token 说「算了」
        check(code == 200, "改配置成功（省 token：意愿压到 0）", d)
        time.time()
        ok, _, _ = await turn(qq, G5, A, "随便聊聊", at_bot=True)
        asks = []
        for _ in range(30):
            asks = [
                x
                for x in (await budget_of(s, hdr)).get("recent") or []
                if x.get("where") == f"群 {G5}" and x.get("kind") == "extra"
            ]
            if asks:
                break
            await asyncio.sleep(0.5)
        await asyncio.sleep(1)
        check(
            ok
            and asks
            and not asks[0]["ok"]
            and asks[0]["by"] == "roll"
            and not [x for x in qq.sent if x[0] == G5 and "试探：" in x[1]],
            "冷场了想试探：先问省 token 模块（extra），它说算了 → 不说",
            (asks, [x for x in qq.sent if x[0] == G5]),
        )
        code, d = await set_config(s, hdr, PHASE3)  # 恢复省 token 配置
        await api_post(s, hdr, {"m": "budget", "do": "full", "hours": -1})
        calls0, llm0 = (
            ((await budget_of(s, hdr)).get("parts") or {}).get("calls", 0),
            llm_total(),
        )
        await api_post(s, hdr, {"m": "rhythm", "op": "lurk", "gk": f"napcat:{G5}"})
        t0 = time.time()
        ok, _, _ = await turn(qq, G3, A, "开个话题：火锅蘸料放啥", at_bot=True)
        check(ok, "@ 它：回了（进在线，0.4 分钟）")
        got = await qq.wait_sent(G3, t0, timeout=20, has="试探：")
        lc = [x for x in line_calls if x["kind"] == "probe"]
        check(
            got and lc,
            f"没人说话 {round(time.time() - t0)} 秒 → 冷场试探发出去了",
            qq.sent[-3:],
        )
        if lc:
            check(
                PERSONA in lc[-1]["system"]
                and "突然安静了几分钟" in lc[-1]["system"]
                and "现在是" in lc[-1]["system"],
                "试探的 system prompt：AstrBot 的默认人格 + 冷场了 + 几点",
                lc[-1]["system"][:400],
            )
            check(
                "老王: 开个话题：火锅蘸料放啥" in lc[-1]["prompt"]
                and "你: 收到第" in lc[-1]["prompt"],
                "试探时给模型看了最近的群聊（「你」是它自己）",
                lc[-1]["prompt"],
            )
        chatter = [
            "嗯嗯",
            "哈哈哈",
            "今天好冷",
            "我刚到家",
            "外面下雨了",
            "楼下好吵",
            "晚饭吃了面条",
            "困了",
            "刚洗完澡",
            "明天周三",
            "楼上装修",
            "好饿",
        ]
        i, got_exit = 0, []
        nsent_probe = len([x for x in qq.sent if x[0] == G3])
        while time.time() - t0 < 60 and not got_exit:
            await qq.say(G3, B if i % 2 else C, chatter[i % len(chatter)])
            i += 1
            got_exit = await qq.wait_sent(G3, t0, timeout=3.0, has="收尾：")
        check(
            got_exit,
            f"在线到点、大家还在聊 → 收尾一句（{round(time.time() - t0)} 秒）",
            qq.sent[-3:],
        )
        g3 = [x for x in qq.sent if x[0] == G3]
        check(
            len(g3) == nsent_probe + 1,
            "大家随便聊的时候（概率 0）没插嘴，只多了收尾那句",
            [x[1] for x in g3],
        )
        await asyncio.sleep(1)
        bs = await budget_of(s, hdr)
        asks = [
            x
            for x in bs.get("recent") or []
            if x.get("where") == f"群 {G3}" and x.get("kind") == "extra"
        ]
        calls1 = (bs.get("parts") or {}).get("calls", 0)
        check(
            len(asks) == 2
            and all(x["ok"] for x in asks)
            and calls1 - calls0 == llm_total() - llm0 >= 2,
            "试探、收尾之前都问了省 token 模块；用掉的 token 也记了（对账：这段时间假模型被叫几次就记几次）",
            (asks, calls1 - calls0, llm_total() - llm0),
        )
        await asyncio.sleep(1)
        ok, _, ks = await turn(qq, G3, B, "你要去哪呀？", expect=False)
        check(not ok and not ks, "收尾后冷却期：接着它的话问「你要去哪呀？」也不接", ks)
        ok, chat, ks = await turn(qq, G3, A, "别走嘛", at_bot=True)
        check(ok, "冷却期被 @：照样回")
        check(
            "收尾：我先去吃饭了 你们聊" in hist_text(chat)
            and "试探：话说回来" in hist_text(chat),
            "主动说的两句（试探、收尾）记进了 AstrBot 的对话记录",
            hist_text(chat)[-600:],
        )
        st_code, d = await api_get(s, hdr, "state")
        st = (d or {}).get("data") or {}
        g3s = {g["g"]: g for g in st.get("groups") or []}.get(str(G3), {})
        check(
            g3s.get("mode") == "收尾冷却" and g3s.get("cool_sec", 0) > 0,
            "被 @ 回了一句也不重新进在线：还是收尾冷却",
            g3s,
        )
        ok, _, ks = await turn(qq, G3, A, "你真走啦？", expect=False)
        check(not ok, "冷却期不带 @ 接着说：不接")
        t3 = st.get("today") or {}
        lines = st.get("lines") or []
        check(
            t3.get("probe", 0) >= 1
            and t3.get("exit", 0) >= 1
            and any(x["kind"] == "exit" for x in lines)
            and any(x["kind"] == "probe" for x in lines),
            f"今天的数：试探 {t3.get('probe')}、收尾 {t3.get('exit')}；面板里有这两句",
            lines[:3],
        )
        await asyncio.sleep(1)
        saved = (
            json.loads(
                (ROOT / "data" / "plugin_data" / PNAME / "state.json").read_text(
                    encoding="utf-8"
                )
            ).get("rhythm")
            or {}
        )
        check(
            (saved.get("stats") or {}).get("exit", 0) >= 1
            and (saved.get("stats") or {}).get("follow", 0) >= 7,
            "统计随时存进了 state.json",
            saved.get("stats"),
        )
        text = log_text()
        check(
            "Traceback" not in text,
            "整个过程 AstrBot 日志里没有 Traceback",
            text[text.find("Traceback") - 300 :][:1500],
        )
        check(
            "[节奏] 群7003 冷场试探 →" in text
            and "[节奏] 群7003 收尾潜水 →" in text
            and "[接话] 群7001" in text,
            "日志里看得到接话、试探、收尾",
        )
        await s.close()
    finally:
        proc.terminate()
        try:
            proc.wait(15)
        except Exception:
            proc.kill()
        await runner.cleanup()
        log.close()

    ok = sum(1 for c, _ in RES if c)
    bad = [m for c, m in RES if not c]
    print("\n" + "=" * 60)
    print(f"  结果：{ok} 项过了" + (f"，{len(bad)} 项没过" if bad else "，全过"))
    for m in bad:
        print("    -", m)
    print("=" * 60)
    if KEEP or bad:
        print("AstrBot 目录留着：", ROOT)
    else:
        shutil.rmtree(ROOT, ignore_errors=True)
    sys.exit(1 if bad else 0)


asyncio.run(main())
