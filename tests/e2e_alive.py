#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
活人感合并后的集成端到端（原来三个插件各自的端到端在 e2e_gather.py / e2e_rhythm.py / e2e_budget.py，每个只开一个模块；这个是四个模块一起开）：
真的 AstrBot（4.29.3）+ 这个插件 + 假 QQ 端（假 NapCat 反向 WebSocket）+ 假模型（OpenAI 兼容，按关键字回：EMPTY 空回 / THINKONLY 只有思考 / LEAK 漏思考 / BOOM 接口报错 / TONE 客服腔）
  1 第一次装上：旁边有旧三个插件的存档和配置 → 自动接上（面板、配置都看得到），旧目录原样不动
  2 三个模块一起干活：接话 + 攒一攒（@ 后马上补一句并成一次）+ 「稍等」多等 + 收尾句不接 + 省 token 对账 + /活人感 指令
  3 兜底：空回补救、只有 <think> 补救、漏 <think> 去掉、接口报错不外发、客服腔（默认关，打开以后去掉）、兜底关掉就不管
  4 每个模块单独关，别的照常；总开关关掉全不管
  5 装了同类插件（假的 group_chat_plus）：重启后攒一攒、群聊节奏自动让路（面板 + 日志写明），省 token、兜底照常；强制开 / 关自动让路
用法：python tests/e2e_alive.py [插件目录 默认上一层] [--keep]
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
SELF, A, B, C, AI, ADMIN = 99999, 30001, 30002, 30003, 50005, 30009
G1, G2, G3, G4 = 7001, 7002, 7003, 7004
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
ROOT = Path(tempfile.mkdtemp(prefix="astrbot-e2e-alive-"))
KIND_LOG = ROOT / "kinds.jsonl"
SECRET = "e2e-secret-0123456789abcdef0123456789"
chat_calls, judge_calls, line_calls, rescue_calls = [], [], [], []
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
    first = first_text(msgs[-1]) if msgs else ""
    if "你在旁观一个 QQ 群" in system:
        judge_calls.append({"system": system, "body": last, "t": time.time()})
        text = JUDGE["answer"]
    elif "突然安静了几分钟" in system:
        line_calls.append(
            {"kind": "probe", "system": system, "prompt": last, "t": time.time()}
        )
        text = "试探：话说回来"
    elif "现在想下线" in system:
        line_calls.append(
            {"kind": "exit", "system": system, "prompt": last, "t": time.time()}
        )
        text = "收尾：我先去吃饭了"
    elif "你上一次没有给出正文" in system:
        rescue_calls.append({"system": system, "prompt": last, "t": time.time()})
        text = "补救：好的这是补上的一句"
    else:
        chat_calls.append(
            {"messages": msgs, "system": system, "last": last, "t": time.time()}
        )
        if "BOOM" in first:
            return web.json_response(
                {"error": {"message": "boom 假的接口报错", "type": "server_error"}},
                status=500,
            )
        if "EMPTY" in first:
            text = ""
        elif "THINKONLY" in first:
            text = "<think>我在想</think>"
        elif "LEAK" in first:
            text = "<think>偷偷想</think>漏了但有正文"
        elif "TONE" in first:
            text = "好的，我来说。希望这些对你有帮助！"
        else:
            text = f"收到第{len(chat_calls)}句"
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


def kinds_since(n):
    return kinds()[n:]


def install_fake_peer():
    """假装装了 group_chat_plus（同类插件）：只有一个空壳，让路检测认得出"""
    hp = ROOT / "data" / "plugins" / "astrbot_plugin_group_chat_plus"
    hp.mkdir(parents=True, exist_ok=True)
    (hp / "metadata.yaml").write_text(
        "name: astrbot_plugin_group_chat_plus\ndesc: e2e 测试用的假同类插件\nversion: v0.0.1\nauthor: e2e\n",
        encoding="utf-8",
    )
    (hp / "main.py").write_text(
        "from astrbot.api.star import Context, Star\n\n\nclass GroupChatPlus(Star):\n    def __init__(self, context: Context, config=None):\n        super().__init__(context)\n",
        encoding="utf-8",
    )


def plugin_defaults():
    schema = json.loads((PLUGIN_SRC / "_conf_schema.json").read_text(encoding="utf-8"))
    return {
        g: {k: v.get("default") for k, v in (gv.get("items") or {}).items()}
        for g, gv in schema.items()
    }


# 整份配置：每次都从默认值起，再叠这套测试要的（rhythm 的随机挑着接关掉、话题词保持默认、接话分数线保持默认）
BASE = {
    "alive": {},
    "gather": {},
    "rhythm": {
        "reply_prob": 0.0,
        "probe_prob": 0.0,
        "exit_line_prob": 0.0,
        "idle_min": 600.0,
        "online_min": 60.0,
        "online_max": 60.0,
        "max_minutes": 120.0,
    },
    "budget": {},
    "guard": {},
    "compat": {},
}
CUR = {}


def conf(**groups):
    c = plugin_defaults()
    for g, v in BASE.items():
        c[g].update(v)
    for g, v in groups.items():
        c[g].update(v)
    return c


OLD_DAY = time.strftime("%Y-%m-%d")


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
    cfg["provider_ltm_settings"]["group_icl_enable"] = True
    cfg["admins_id"] = [str(ADMIN)]
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
    # ── 旧三个插件留下的东西：存档 + 配置（新插件第一次装上时应该自己接上）──
    pdir = ROOT / "data" / "plugin_data"
    now = int(time.time())
    olds = {
        "gather": {
            "stats": {"day": OLD_DAY, "merges": 5, "saved": 7, "msgs": 12, "turns": 5},
            "recent": [
                {
                    "t": now - 600,
                    "where": "私聊",
                    "who": "旧用户甲",
                    "n": 3,
                    "waited": 2.1,
                    "pics": 0,
                    "preview": "旧的合并记录",
                }
            ],
        },
        "rhythm": {
            "stats": {"day": OLD_DAY, "follow": 7, "chat": 2},
            "decisions": [],
            "lines": [],
        },
        "budget": {
            "v": 1,
            "ledger": [],
            "full": None,
            "reset": None,
            "day": {},
            "skips": {},
            "recent": [
                {
                    "t": now - 300,
                    "kind": "direct",
                    "ok": True,
                    "by": "roll",
                    "p": 0.9,
                    "roll": 0.1,
                    "who": "旧用户乙",
                    "where": "群 1",
                    "boost": 0,
                }
            ],
        },
    }
    for n, v in olds.items():
        d = pdir / f"astrbot_plugin_fatfish_{n}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(
            json.dumps(v, ensure_ascii=False), encoding="utf-8"
        )
    cdir = ROOT / "data" / "config"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "astrbot_plugin_fatfish_gather_config.json").write_text(
        json.dumps({"base_sec": 2.5, "private": False}), encoding="utf-8"
    )
    (cdir / "astrbot_plugin_fatfish_rhythm_config.json").write_text(
        json.dumps({"window_sec": 150.0, "reply_prob": 0.0}), encoding="utf-8"
    )
    (cdir / "astrbot_plugin_fatfish_budget_config.json").write_text(
        json.dumps({"half_usd": 0.8}), encoding="utf-8"
    )


def mine_tb(text):
    """日志里带着本插件文件路径的堆栈（别的堆栈是故意造的模型报错，AstrBot 自己打的）"""
    out = []
    for blk in text.split("Traceback (most recent call last)")[1:]:
        head = blk[:6000]
        if "astrbot_plugin_fatfish_alive" in head.replace("\\", "/"):
            out.append(head[:300])
    return out


def log_text():
    return (ROOT / "astrbot_stdout.log").read_text(encoding="utf-8", errors="replace")


def sent_to(gid, since):
    return [x for x in qq_ref[0].sent if x[0] == gid and x[2] >= since]


qq_ref = [None]


async def turn(qq, gid, uid, text="", expect=True, quiet=3.5, **kw):
    """说一句 → 等机器人回（expect=False 时等 quiet 秒确认没回）→ (回了的话[], 这一轮的回话请求[], 这一轮记下的 kind[])"""
    nc, nk, t0 = len(chat_calls), len(kinds()), time.time()
    await qq.say(gid, uid, text, **kw)
    got = await qq.wait_sent(gid, t0, timeout=15 if expect else quiet)
    await asyncio.sleep(0.8 if got else 0.3)
    return (
        [x[1] for x in qq.sent if x[0] == gid and x[2] >= t0],
        chat_calls[nc:],
        kinds()[nk:],
    )


async def api_get(s, hdr, path="state"):
    async with s.get(
        f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/extensions/{PNAME}/{path}",
        headers=hdr,
    ) as r:
        d = await r.json(content_type=None)
        return r.status, ((d or {}).get("data") or {})


async def api_post(s, hdr, body):
    async with s.post(
        f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/extensions/{PNAME}/action",
        headers=hdr,
        json=body,
    ) as r:
        return r.status, await r.json(content_type=None)


async def set_config(s, hdr, **groups):
    cfg = conf(**groups)
    async with s.put(
        f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/{PNAME}/config",
        headers=hdr,
        json={"config": cfg},
    ) as r:
        d = await r.json(content_type=None)
    await asyncio.sleep(1.8)
    return r.status, d


def llm_total():
    return len(chat_calls) + len(judge_calls) + len(line_calls) + len(rescue_calls)


async def start(env, log):
    proc = subprocess.Popen(
        [ASTRBOT, "run"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT
    )
    qq = FakeQQ()
    await qq.connect()
    qq_ref[0] = qq
    for _ in range(120):
        t = log_text()
        if PNAME in t and "astrbot_plugin_e2e_kindprobe" in t and "[活人感] v" in t:
            break
        await asyncio.sleep(0.5)
    await asyncio.sleep(3)
    return proc, qq


async def stop(proc):
    proc.terminate()
    try:
        proc.wait(20)
    except Exception:
        proc.kill()


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
    for k in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        env[k] = "http://127.0.0.1:9"
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    env["ASTRBOT_DISABLE_METRICS"] = "1"
    log = open(ROOT / "astrbot_stdout.log", "w", encoding="utf-8")
    token = jwt.encode(
        {"username": "astrbot", "exp": int(time.time()) + 3600},
        SECRET,
        algorithm="HS256",
    )
    hdr = {"Authorization": f"Bearer {token}"}
    proc, qq, s = None, None, None
    try:
        proc, qq = await start(env, log)
        s = aiohttp.ClientSession()
        text = log_text()
        check(
            "[活人感] v0.1.0 已启用" in text and "Traceback" not in text,
            "AstrBot 起来了、活人感加载了、没有报错",
            text[-1500:],
        )

        # ═════════ 1 第一次装：旧存档、旧配置自己接上 ═════════
        print("\n[1 第一次装上：旧三个插件的存档和配置自己接上]")
        code, st = await api_get(s, hdr)
        al = st.get("alive") or {}
        check(
            code == 200
            and sorted((al.get("migrated") or {}).get("from") or [])
            == ["budget", "gather", "rhythm"]
            and not (al.get("migrated") or {}).get("failed"),
            "面板：迁移了三块旧存档",
            al.get("migrated"),
        )
        check(
            (st["gather"]["today"].get("merges") or 0) >= 5
            and any(x.get("who") == "旧用户甲" for x in st["gather"]["recent"]),
            "攒一攒：今天的数和最近合并接着旧的",
            st["gather"]["today"],
        )
        check(
            (st["rhythm"]["today"].get("follow") or 0) >= 7,
            "群聊节奏：今天的数接着旧的",
            st["rhythm"]["today"],
        )
        check(
            any(x.get("who") == "旧用户乙" for x in st["budget"]["recent"]),
            "省 token：最近的决定接着旧的",
            st["budget"]["recent"][:2],
        )
        cfg = json.loads(
            (ROOT / "data" / "config" / f"{PNAME}_config.json").read_text(
                encoding="utf-8-sig"
            )
        )
        check(
            cfg["gather"]["base_sec"] == 2.5
            and cfg["gather"]["private"] is False
            and cfg["rhythm"]["window_sec"] == 150.0
            and cfg["budget"]["half_usd"] == 0.8,
            "旧配置里改过的项搬进了新分组（攒一攒 / 群聊节奏 / 省 token）",
            {g: cfg.get(g) for g in ("gather", "rhythm", "budget")},
        )
        check(
            st["gather"]["config"]["base_sec"] == 2.5
            and st["gather"]["config"]["private"] is False,
            "而且已经生效",
            st["gather"]["config"],
        )
        check(
            all(al["modules"][n]["on"] for n in ("gather", "rhythm", "budget", "guard"))
            and not al["yield"]["found"],
            "四个模块都在工作、没有让路",
            al.get("modules"),
        )
        check(
            (
                ROOT
                / "data"
                / "plugin_data"
                / "astrbot_plugin_fatfish_gather"
                / "state.json"
            ).exists(),
            "旧存档的目录没动（原样留着）",
        )
        code, d = await api_post(s, hdr, {"m": "budget", "do": "full", "hours": -1})
        check(
            code == 200, "（测试用）先把省 token 的「火力全开」一直开着，才不掷骰子", d
        )

        # ═════════ 2 三个模块一起干活 ═════════
        print("\n[2 三个模块一起干活（同一个群里）]")
        base_merges = st["gather"]["today"]["merges"]
        said, chats, ks = await turn(qq, G1, A, "大家好", at_bot=True)
        check(
            len(said) == 1 and len(chats) == 1,
            "@ 它：回了一句、叫了一次模型",
            (said, len(chats)),
        )
        said, chats, ks = await turn(qq, G1, A, "你刚才说的啥意思")
        check(
            len(said) == 1 and len(chats) == 1 and ks and ks[-1]["kind"] == "follow",
            "接话：不带 @ 接着说 → 接了（群聊节奏），攒一攒等了一下也没回两遍",
            (said, len(chats), ks),
        )
        t0 = time.time()
        said, chats, ks = await turn(qq, G1, A, "好了", expect=False, quiet=5.0)
        check(
            not said and not chats,
            "收尾句「好了」：不接（以前会因为「它刚回的就是他」接上）",
        )
        t0 = time.time()
        await qq.say(G2, B, "问你个事", at_bot=True)
        await asyncio.sleep(0.6)
        await qq.say(G2, B, "就是火锅怎么吃")
        got = await qq.wait_sent(G2, t0, timeout=15)
        await asyncio.sleep(1.5)
        cs = [c for c in chat_calls if c["t"] >= t0 and "问你个事" in c["last"]]
        check(
            got
            and len(sent_to(G2, t0)) == 1
            and len(cs) == 1
            and "问你个事\n就是火锅怎么吃" in cs[0]["last"],
            "攒一攒：@ 后马上补了一句 → 并成一次、只回一遍",
            ([c["last"][-120:] for c in cs], sent_to(G2, t0)),
        )
        t0 = time.time()
        await qq.say(G3, C, "稍等", at_bot=True)
        await asyncio.sleep(4.5)  # 普通的话 3.5 秒就回了；「稍等」要多等
        mid = [c for c in chat_calls if c["t"] >= t0 and "稍等" in c["last"]]
        await qq.say(G3, C, "我想问火锅蘸料放什么")
        got = await qq.wait_sent(G3, t0, timeout=15)
        await asyncio.sleep(1.5)
        cs = [c for c in chat_calls if c["t"] >= t0 and "稍等" in c["last"]]
        check(
            not mid
            and len(cs) == 1
            and "稍等\n我想问火锅蘸料放什么" in cs[0]["last"]
            and len(sent_to(G3, t0)) == 1,
            "「稍等」：4.5 秒了还没回；他接着说了 → 两句并成一次回",
            (len(mid), [c["last"][-120:] for c in cs]),
        )
        code, st = await api_get(s, hdr)
        check(
            st["gather"]["today"]["merges"] >= base_merges + 2
            and st["rhythm"]["today"]["follow"] >= 8,
            f"面板：攒一攒合并 {st['gather']['today']['merges']} 次、群聊节奏接话 {st['rhythm']['today']['follow']} 次",
            (st["gather"]["today"], st["rhythm"]["today"]),
        )
        await asyncio.sleep(1.0)
        parts = st["budget"].get("parts") or {}
        check(
            parts.get("calls") == llm_total()
            and (parts.get("out") or {}).get("tok") == 10 * llm_total(),
            "对账：假模型总共被叫几次、省 token 就记了几次（回话 + 判断）",
            (parts.get("calls"), llm_total(), parts),
        )

        print("\n[2b 指令]")
        said, chats, ks = await turn(qq, G1, ADMIN, "/活人感")
        check(
            said
            and all(x in said[0] for x in ("攒一攒", "群聊节奏", "省 token", "兜底"))
            and not chats,
            "管理员 /活人感：四个模块的状态",
            said,
        )
        said, chats, ks = await turn(qq, G1, A, "/活人感", expect=False)
        check(
            not chats and (not said or "权限" in said[0]),
            "非管理员 /活人感：用不了",
            said,
        )
        said, chats, ks = await turn(qq, G1, ADMIN, "/省token")
        check(
            said and "回复意愿" in said[0],
            "/省token 还在（原来省 token 插件的指令）",
            said,
        )

        # ═════════ 3 兜底 ═════════
        print("\n[3 兜底：报错不外发 / 空回补救 / 只有 <think> / 漏 <think> / 客服腔]")
        nb = len(rescue_calls)
        said, chats, ks = await turn(qq, G4, A, "EMPTY 在吗", at_bot=True)
        check(
            len(said) == 1
            and "补救：" in said[0]
            and "错误" not in said[0]
            and len(rescue_calls) == nb + 1,
            "模型什么都没回（AstrBot 4.29.3 会把它当成报错发出来）：报错拦下、补调一次、发出去的是补救的话",
            (said, len(rescue_calls) - nb),
        )
        said, chats, ks = await turn(qq, G4, A, "THINKONLY 在吗", at_bot=True)
        check(
            len(said) == 1
            and "补救：" in said[0]
            and "<think" not in said[0]
            and "我在想" not in said[0]
            and len(rescue_calls) == nb + 2,
            "只有 <think> 没正文（AstrBot 什么都不会发）：模型回完时补调一次、替它发出去，思考过程没漏",
            (said, len(rescue_calls) - nb),
        )
        said, chats, ks = await turn(qq, G4, A, "LEAK 在吗", at_bot=True)
        check(
            len(said) == 1
            and said[0].strip() == "漏了但有正文"
            and "偷偷想" not in said[0],
            "正文前面漏了 <think>：去掉、只发正文",
            said,
        )
        said, chats, ks = await turn(
            qq, G4, A, "BOOM 在吗", at_bot=True, expect=False, quiet=6.0
        )
        bad = [
            x
            for x in said
            if "Error" in x or "错误" in x or "Traceback" in x or "失败" in x
        ]
        check(not bad, "模型接口报错：报错的话没发到群里", said)
        text = log_text()
        check(
            "框架报错没发出去" in text or not said,
            "（日志里写了拦下来的原因，或者框架本来就没发）",
            [x for x in text.splitlines() if "框架报错" in x][-1:],
        )
        said, chats, ks = await turn(qq, G4, A, "TONE 在吗", at_bot=True)
        check(
            len(said) == 1 and "希望这些对你有帮助" in said[0],
            "去 AI 腔默认关：原样发",
            said,
        )
        code, d = await set_config(s, hdr, guard={"strip_ai_tone": True})
        check(
            code == 200 and (d or {}).get("status") == "ok",
            "WebUI 里打开「去 AI 腔」（热重载）",
            d,
        )
        said, chats, ks = await turn(qq, G4, A, "TONE 再来", at_bot=True)
        check(
            len(said) == 1
            and "希望" not in said[0]
            and "好的，我来说" in said[0]
            and chats
            and "客服腔" in chats[0]["last"],
            "打开以后：回话前提醒别用客服腔、发出去的话去掉了结尾那句",
            (said, chats and chats[0]["last"][-200:]),
        )
        code, st = await api_get(s, hdr)
        g = st["guard"]["today"]
        check(
            g.get("rescued", 0) >= 2
            and g.get("tone", 0) >= 1
            and g.get("errors", 0) >= 1,
            f"面板：兜底今天 {g}",
            g,
        )
        code, d = await set_config(s, hdr, guard={"enable": False})
        said, chats, ks = await turn(
            qq, G4, A, "EMPTY 再来", at_bot=True, expect=False, quiet=5.0
        )
        check(
            said and "LLM 响应错误" in said[0],
            "兜底关掉：不管了，AstrBot 自己的报错原样发出来（证明前面是兜底拦的）",
            said,
        )

        # ═════════ 4 每个模块单独关，别的照常 ═════════
        print("\n[4 每个模块单独关]")
        BASE["guard"] = {}
        code, d = await set_config(s, hdr, gather={"enable": False})
        t0 = time.time()
        await qq.say(G2, B, "关攒一攒第一句", at_bot=True)
        await asyncio.sleep(0.6)
        await qq.say(G2, B, "关攒一攒第二句")
        await asyncio.sleep(6.0)
        cs = [c for c in chat_calls if c["t"] >= t0 and "关攒一攒" in c["last"]]
        check(
            len(cs) == 1 and "关攒一攒第一句\n关攒一攒第二句" not in cs[0]["last"],
            "攒一攒关掉：不并（第二句不带 @、AstrBot 本来就不回它）",
            [c["last"][-80:] for c in cs],
        )
        said, chats, ks = await turn(qq, G1, A, "喂", at_bot=True)
        said, chats, ks = await turn(qq, G1, A, "你还在吗")
        check(said and ks and ks[-1]["kind"] == "follow", "…但群聊节奏照常接话", ks)
        code, d = await set_config(s, hdr, rhythm={"enable": False})
        said, chats, ks = await turn(qq, G1, A, "喂喂", at_bot=True)
        said, chats, ks = await turn(qq, G1, A, "你还在吗你", expect=False, quiet=6.0)
        check(not said and not ks, "群聊节奏关掉：不带 @ 接着说不接", (said, ks))
        t0 = time.time()
        await qq.say(G2, B, "只关节奏第一句", at_bot=True)
        await asyncio.sleep(0.6)
        await qq.say(G2, B, "只关节奏第二句")
        await qq.wait_sent(G2, t0, timeout=15)
        await asyncio.sleep(1.5)
        cs = [c for c in chat_calls if c["t"] >= t0 and "只关节奏" in c["last"]]
        check(
            len(cs) == 1 and "只关节奏第一句\n只关节奏第二句" in cs[0]["last"],
            "…但攒一攒照常并",
            [c["last"][-80:] for c in cs],
        )
        code, d = await set_config(s, hdr, alive={"enabled": False})
        t0 = time.time()
        await qq.say(G2, B, "总开关第一句", at_bot=True)
        await asyncio.sleep(0.6)
        await qq.say(G2, B, "总开关第二句")
        await asyncio.sleep(6.0)
        cs = [c for c in chat_calls if c["t"] >= t0 and "总开关" in c["last"]]
        said, chats, ks = await turn(
            qq, G4, A, "EMPTY 总开关", at_bot=True, expect=False, quiet=5.0
        )
        check(
            len(cs) == 1
            and "总开关第一句\n总开关第二句" not in cs[0]["last"]
            and said
            and "LLM 响应错误" in said[0],
            "总开关关掉：不并、不兜底（AstrBot 自己照常回、报错也是它自己的）",
            ([c["last"][-60:] for c in cs], said),
        )
        code, st = await api_get(s, hdr)
        check(
            not st["alive"]["enabled"]
            and not any(m["on"] for m in st["alive"]["modules"].values()),
            "面板：总开关关着、四个模块都没在工作",
            st["alive"]["modules"],
        )
        code, d = await set_config(s, hdr)
        check(code == 200, "全部恢复")

        text = log_text()
        check(
            not mine_tb(text),
            "第一段 AstrBot 日志里没有本插件的报错堆栈（模型空回 / 接口报错是故意造的，AstrBot 自己会打它的堆栈）",
            mine_tb(text)[:3],
        )

        # ═════════ 5 装了同类插件：让路 ═════════
        print("\n[5 装了 group_chat_plus（同类插件）：重启后自动让路]")
        await s.close()
        await stop(proc)
        install_fake_peer()
        log.write("\n===== 第二轮：装了假的 group_chat_plus =====\n")
        log.flush()
        proc, qq = await start(env, log)
        s = aiohttp.ClientSession()
        for _ in range(20):  # 所有插件加载完就查同类插件（不用等 20 秒）
            if "[兼容] 因为装了" in log_text()[log_text().find("第二轮") :]:
                break
            await asyncio.sleep(0.5)
        code, st = await api_get(s, hdr)
        al = st["alive"]
        mods = al["modules"]
        if os.environ.get("ALIVE_DUMP"):
            Path(os.environ["ALIVE_DUMP"]).write_text(
                json.dumps(st, ensure_ascii=False), encoding="utf-8"
            )
        check(
            [x["label"] for x in al["yield"]["found"]] == ["group_chat_plus"]
            and mods["gather"]["yield"]
            and mods["rhythm"]["yield"]
            and not mods["gather"]["on"]
            and not mods["rhythm"]["on"]
            and mods["budget"]["on"]
            and mods["guard"]["on"],
            "面板：认出 group_chat_plus；攒一攒、群聊节奏让路；省 token、兜底照常",
            (al["yield"], {k: (v["on"], v["yield"]) for k, v in mods.items()}),
        )
        check(
            any(x["found"] for x in al["yield"]["log"]),
            "让路记录里写了一笔",
            al["yield"]["log"],
        )
        text = log_text()
        check(
            "[兼容] 因为装了 group_chat_plus" in text,
            "日志里写明因为装了谁",
            [x for x in text.splitlines() if "兼容" in x][-2:],
        )
        check(
            (st["gather"]["today"].get("merges") or 0) >= 7,
            "重启以后今天的数还在（存档接上了、没有再迁一遍）",
            st["gather"]["today"],
        )
        await api_post(s, hdr, {"m": "budget", "do": "full", "hours": -1})
        t0 = time.time()
        await qq.say(G2, B, "让路第一句", at_bot=True)
        await asyncio.sleep(0.6)
        await qq.say(G2, B, "让路第二句")
        await asyncio.sleep(6.0)
        cs = [c for c in chat_calls if c["t"] >= t0 and "让路" in c["last"]]
        check(
            len(cs) == 1 and "让路第一句\n让路第二句" not in cs[0]["last"],
            "让路：不并（交给那个插件）",
            [c["last"][-60:] for c in cs],
        )
        said, chats, ks = await turn(qq, G1, A, "你好呀", at_bot=True)
        said, chats, ks = await turn(
            qq, G1, A, "你刚才说的啥意思", expect=False, quiet=6.0
        )
        check(
            not said and not ks,
            "让路：不带 @ 接着说不接（不会跟那个插件抢着回）",
            (said, ks),
        )
        nb = len(rescue_calls)
        said, chats, ks = await turn(qq, G4, A, "EMPTY 让路时", at_bot=True)
        check(
            len(said) == 1 and "补救：" in said[0] and len(rescue_calls) == nb + 1,
            "兜底照常（空回补救）",
            said,
        )
        await asyncio.sleep(1)
        code, st = await api_get(s, hdr)
        parts = st["budget"].get("parts") or {}
        check(parts.get("calls", 0) >= 3, "省 token 照常记账", parts)
        async with s.get(
            f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/{PNAME}/views/panel",
            headers=hdr,
        ) as r:
            check(r.status == 200, "面板页取得到")

        print("\n[5b 想让路也可以不让：强制开攒一攒 / 关掉自动让路]")
        code, d = await set_config(s, hdr, compat={"force_gather": True})
        t0 = time.time()
        await qq.say(G2, B, "强制第一句", at_bot=True)
        await asyncio.sleep(0.6)
        await qq.say(G2, B, "强制第二句")
        await qq.wait_sent(G2, t0, timeout=15)
        await asyncio.sleep(1.5)
        cs = [c for c in chat_calls if c["t"] >= t0 and "强制" in c["last"]]
        code, st = await api_get(s, hdr)
        check(
            len(cs) == 1
            and "强制第一句\n强制第二句" in cs[0]["last"]
            and st["alive"]["modules"]["gather"]["on"]
            and not st["alive"]["modules"]["rhythm"]["on"],
            "「强制开攒一攒」：攒一攒照常并、群聊节奏还让着路",
            (
                [c["last"][-60:] for c in cs],
                {k: v["on"] for k, v in st["alive"]["modules"].items()},
            ),
        )
        code, d = await set_config(s, hdr, compat={"auto_yield": False})
        code, st = await api_get(s, hdr)
        said, chats, ks = await turn(qq, G1, A, "再来", at_bot=True)
        said, chats, ks = await turn(qq, G1, A, "你还在吗你")
        check(
            all(st["alive"]["modules"][n]["on"] for n in ("gather", "rhythm"))
            and said
            and ks
            and ks[-1]["kind"] == "follow",
            "关掉「自动让路」：两个模块都恢复，接话也回来了",
            (ks, {k: v["on"] for k, v in st["alive"]["modules"].items()}),
        )

        text = log_text()
        check(
            not mine_tb(text[text.find("第二轮") :]),
            "第二轮 AstrBot 日志里没有本插件的报错堆栈",
            mine_tb(text[text.find("第二轮") :])[:3],
        )
    finally:
        if s is not None:
            await s.close()
        if proc is not None:
            await stop(proc)
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
