# 大肥鱼 · 活人感

AstrBot 插件。让机器人在 QQ 里像个真人：不一条一条机械地回、不是人人说话都接、花得多了就少说点、出了岔子也不会把报错发到群里。

由原来三个插件合并而成，再加一层兜底：

| 模块 | 干什么 | 原来的插件 |
|---|---|---|
| 攒一攒 | 同一个人连着发好几条：等他停下来，并成一件事只回一次（图也带上）；说了「稍等 / 我还没说完」就多等一阵 | astrbot_plugin_fatfish_gather |
| 群聊节奏 | 没 @ 但在接它的话也接；说上话以后在线聊一阵、挑着接；冷场试探一句；到点说句收尾话再潜水；「好了 / 就这样 / 睡了」不接 | astrbot_plugin_fatfish_rhythm |
| 省 token | 把最近 24 小时花的钱算成「回复意愿」，花得越多越不爱回；私聊照回、被 @ 几乎都回，没人找它时省得最多；管理员可以「火力全开」 | astrbot_plugin_fatfish_budget |
| 兜底（新） | 框架报错不发到聊天里；模型空回 / 只有 `<think>` 就补调一次；可选去掉「作为一个AI」「希望对你有帮助」这类客服腔 | — |

一个面板看全部（总览 + 三个标签页），一份配置（按模块分组）。

## 安装

- **插件市场**：WebUI → 插件 → 插件市场，搜「大肥鱼」或「活人感」。
- **从链接安装**：WebUI → 插件 → 安装 → 从链接安装，填 `https://github.com/Vegra85/astrbot_plugin_fatfish_alive`。
- **从文件安装**：WebUI → 插件 → 安装 → 从文件安装，选这个仓库的 zip。

要求：AstrBot ≥ 4.29（在 4.29.3 上测过）；QQ（aiocqhttp / NapCat）。

## 装了同类插件会自动让路

加载时检查有没有装并启用 `group_chat_plus` / `SpectreCore` / `Heartflow` / `natural_followup` / `jiehua`。装了任何一个，**攒一攒和群聊节奏自动让路**（不然两个都在主动接话，会重复回），只保留省 token 和兜底。面板的「让路情况」和日志都会写明是因为装了谁。

- 想不让路：配置 → 兼容 → 「强制开攒一攒」/「强制开群聊节奏」，或关掉「自动让路」。
- 想让别的插件也触发让路：配置 → 兼容 → 「另外要让路的插件」，填目录名里的一段（至少 3 个字）。

## 从旧三个插件换过来

第一次加载时自动做两件事（只读旧的，不删、不改旧目录）：

1. 把旧三个插件的存档（`plugin_data/astrbot_plugin_fatfish_{gather,rhythm,budget}/state.json`）搬进新插件的 `state.json`；
2. 把旧三个插件的配置（`config/astrbot_plugin_fatfish_{gather,rhythm,budget}_config.json`）里改过的项搬进对应分组（新插件里已经改过的不动）。

迁完以后在存档里打一个标记，不会再迁。迁移的结果在面板「总览 → 旧存档迁移」里看。换完确认没问题，再把旧三个插件从 `data/plugins` 里删掉（**不要两边一起开**，它们会同时在接话）。

## 配置

`_conf_schema.json` 分六组，WebUI 里每一项都有中文说明和提示：

- **总开关**：`alive.enabled`，关了整个插件不做事。
- **攒一攒**：私聊 / 群里攒不攒、基础等几秒、只有图多等几秒、问句只等几秒、最多等几秒、「稍等」最多等几秒、不攒的群……
- **群聊节奏**：接话的窗口和分数线、最多连着接几轮、在线一阵的时长和挑着接的概率、冷场试探、收尾和冷却、「收尾句不接」「话题词没对上扣分」……
- **省 token**：价格、峰谷价、half / 形状 / 底线、各场合的次方、不限的 QQ 号、火力全开默认几小时……
- **兜底**：拦报错、空回补救、补救超时、去 AI 腔（默认关）。
- **兼容**：自动让路、强制开、另外要让路的插件。

每个模块都有自己的 `enable`：关掉一个，别的照常。

## 指令（只有 AstrBot 管理员能用）

- `/活人感`：四个模块现在开没开、有没有在让路、今天的数。
- `/省token`、`/省token 火力全开 [小时数|一直]`、`/省token 省着点`、`/省token 重新算`：跟原来的省 token 插件一样。

## 给别的插件用

跟原来的省 token 插件一样，三个公开方法还在（插件名换成 `astrbot_plugin_fatfish_alive`）：`record_usage(resp)`、`ask(kind, conv, uid, name, where)`、`full_on()`。别的插件在 `event` 上写 `event.set_extra("fatfish_kind", "follow" | "chat" | "extra")`，省 token 就按这个场合算。群聊节奏会去问认 AI 插件（`astrbot_plugin_fatfish_aiguard`）这个群里谁是别家的 AI，没装就读它的 `state.json`。

## 测试

```
python tests/test_gather.py        # 攒一攒单测 45
python tests/test_rhythm.py        # 群聊节奏单测 83
python tests/test_budget.py        # 省 token 单测 75
python tests/test_alive.py         # 合并新增的单测 118（迁移 / 让路 / 兜底 / 稍等 / 收尾句 / 话题词）
python tests/e2e_gather.py         # 以下四个是真 AstrBot 4.29.3 + 假 QQ + 假模型的端到端（各 100 秒左右）
python tests/e2e_rhythm.py
python tests/e2e_budget.py
python tests/e2e_alive.py          # 四个模块一起开 + 装了同类插件让路
```

单测不用装 AstrBot；端到端要装了 astrbot（测过 4.29.3）的 Python，另外要 aiohttp、pyjwt、websockets、Pillow。都只在临时文件夹里跑，不连外网。

## 已知限制

- 「模型回完什么都没有」在 AstrBot 4.29.3 里是被当成报错发出来的（`EmptyModelOutputError`），兜底会拦下并补调一次；「只有 `<think>`」会变成一条空消息，兜底在模型回完时补调并替它发出去。这两处都是照 4.29.3 的行为做的，升级 AstrBot 以后行为可能变。
- 框架里有些报错是直接 `event.send` 发出去的，不经过「发送前」那一步，拦不住（目前认得的主要报错都经过）。
- 兜底的补救会多叫一次模型（很轻：人设 + 这条消息 + 群里最近几句），计入省 token 的账。

## 许可

MIT
