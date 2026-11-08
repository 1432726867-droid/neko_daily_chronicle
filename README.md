# 对话编年史 · neko_daily_chronicle

把 N.E.K.O 里每天的对话**完整留下来**：按日期编成小说式的**章节**，并为每个章节另外写一篇**日记**。

* 原始记录只追加、不覆盖 —— 换模型、改排版、修 bug 都不会丢掉已经记下的对话；
* 章节保留当天原话，程序只补时间与场景说明，不做二次创作；
* 日记优先交给语言模型写；模型不可用时自动降级成本地统计摘要，记录不中断。

插件按 [N.E.K.O 插件开发文档](https://project-neko.online/zh-CN/plugins/) 编写，
使用 `NekoPluginBase` + `@message` / `@timer_interval` / `@plugin_entry` / `@llm_tool` / Hosted TSX UI。

---

## 产出物长什么样

真实输出（由 `python tools/make_demo.py` 生成，代码路径与运行时完全一致）：

* 章节示例：[examples/chapters/2026-01-01.md](examples/chapters/2026-01-01.md)
* 日记示例：[examples/diary/2026-01-01.md](examples/diary/2026-01-01.md)

章节节选：

```markdown
# 第 1 章 · 2026-01-01

> 共 6 句 ｜ 08:00 – 15:14（7 小时 14 分）
> 出场：主人（3）、喵喵（3）

## 一、清晨 · 08:00

**主人**：早安呀，今天想出去走走

**喵喵**：早安～那我们去河边吧，今天有太阳

## 二、上午 · 09:06

*（和上一段隔了 1 小时 6 分）*

**主人**：河边的风好大，但是很舒服
```

日记节选（本地摘要模式）：

```markdown
# 2026-01-01 · 今日纪要

> 😊 心情推测：开心 ｜ 关键词：早安 · 河边 · 照片 · 舒服
>
> 本地统计摘要（未调用语言模型）：下面出现的词句全部来自当天原话。

## 今日概览
| 项目 | 内容 |
| --- | --- |
| 时间跨度 | 08:00 – 15:14（7 小时 14 分） |
| 对话条数 | 6 句（主人 3 ｜ 喵喵 3） |
```

运行时目录结构（`<用户数据根目录>/plugins/neko_daily_chronicle/data/`）：

```
raw/2026-01-01.jsonl        # 唯一的事实来源：一行一条对话
chapters/2026-01-01.md      # 小说式章节
chapters/README.md          # 章节目录
diary/2026-01-01.md         # 每章一篇日记
diary/README.md             # 日记目录
state.json                  # 运行状态
```

---

## 它是怎么工作的

```
@message(source="chat")  ─┐
                          ├─→ 归一化 + 指纹去重 ─→ raw/YYYY-MM-DD.jsonl（只追加）
@timer_interval(60s) 轮询 ─┘                              │
   bus.memory（用户消息）                                  │
   bus.conversations（完整轮次）                           ▼
                                        章节生成（纯函数，可随时重算）→ chapters/*.md
                                                   │
                                                   ▼
                          模型可用？ ──是──→ 提示词 → /chat/completions → 日记
                                     └─否──→ 本地统计摘要（关键词/心情/片段）→ 日记
```

* **两条采集来源**：`@message` 实时接住每条聊天消息；定时器再向消息总线
  （`bus.memory` + `bus.conversations`）补录一次。两条来源按"精确指纹 + 时间桶指纹"
  去重，所以既不会漏，也不会重复。
* **原始记录先行**：章节与日记都是原始记录的派生结果，删掉重算即可。
  按天索引章节序号（第 N 章 = 第 N 个有记录的日子）。
* **后台结算**：跨过零点后，先自动整理昨天的备忘录，再把昨天（以及任何缺日记的
  历史日期）生成章节 + 每日日记；全程不向聊天发送提示。想看每日日记时，发送
  「爱妻日记」+日期（如「爱妻日记 2026-10-22」），插件会把那篇日记单独递给猫娘。
* **并发安全**：定时器各自跑在独立线程里，`@message` 处理器也可能并发，
  所有落盘都走同一把锁；写文件先写 `.tmp` 再 `os.replace`，避免半截文件。

---

## 安装

### 方式 A：开发模式（改代码即生效，推荐先这样验证）

1. 打开 N.E.K.O 的**插件管理器** → 左侧「服务器日志」下方的**开发模式** → 开启开关；
2. 点**加载未打包插件**，选择本插件源码目录（包含 `plugin.toml` 的那一层）：

   ```
   D:\dsh\neko_daily_chronicle
   ```

3. 点**校验**，核对名称 / ID / 版本 / 入口 / 路径，再点**加载未打包插件**；
4. 之后每次改完代码，在开发插件卡片上点**重载**即可。

> 源码目录名必须与插件 ID 一致（`neko_daily_chronicle`）。要移动目录就整体改名，
> 名字必须保持不变，否则 `entry` 解析不到。

### 方式 B：打包给别的机器安装

打包出来的 `.neko-plugin` 位于 `dist/neko_daily_chronicle.neko-plugin`，
在目标机器的插件导入入口安装即可。

> **注意**：`.neko-plugin` 里的 `payload_hash` 是宿主自己的算法，外部无法可靠复现；
> 而且宿主**只肯打包位于它自己插件根目录里的插件**（`/plugin-cli/build` 对其他路径
> 直接返回 404）。所以重新打包要用下面的官方路径，不要手搓 zip：

```bash
# N.E.K.O 正在运行时，在插件目录下执行（会自动同步源码 → 调宿主打包 → 校验哈希 → 复制到 dist/）
python tools/package_plugin.py
```

等价的手工路径：插件管理器 →「开发模式」→「加载未打包插件」（选本插件目录）→ 卡片上的**打包**。

---

## 面板与入口

### Hosted UI 面板（`ui/panel.tsx`）

* 顶部四张统计卡：记录天数 / 已生成章节 / 已生成日记 / 今天句数；
* 一条状态提示：当前用的是语言模型还是本地摘要，以及原因；
* 日期表格：每天句数、心情、是否已有章节与日记，每行一个「查看这一天」按钮；
* 下方显示所选日期的章节正文与日记正文（过长时提示打开 Markdown 文件）；
* 两个卡片显示文件位置与模型状态（含宿主配置文件路径、是否已配置密钥）。

### 运行时入口（Agent / 插件管理器可直接调用）

| 入口 | 作用 |
| --- | --- |
| `status` | 天数、章节/日记数量、今天句数、模型状态、数据目录 |
| `harvest_now` | 立刻从消息总线补录一次 |
| `inspect_capture` | 排障：打印消息总线返回的字段名与角色识别结果（不写入任何记录） |
| `build_day` | 生成指定日期的章节（`date`、`force`、`with_llm`、`with_diary`） |
| `build_today` | 生成今天的章节，可选同时写日记（默认不写） |
| `generate_diary` | **立即生成**某天的日记（以猫娘第一人称写当天发生的事与心情） |
| `build_all` | 按原始记录重算全部日期（`with_diary`、`limit`） |
| `list_days` | 列出已有日期及其章节/日记状态 |
| `read_chapter` / `read_diary` | 读取某天正文（默认不塞进模型上下文，只回元信息） |
| `read_day` | 读某天的对话原文，带序号（`date`、`start`、`count`） |
| `get_turn` | 按序号取某一条原文（`index`、`date`） |
| `recent` | 最近的几条原文（`count`、`date`） |
| `search_chat` | 在**对话原文**里检索，返回日期/序号/片段（`query`、`limit`、`days`） |
| `search` | 在章节与日记里做全文检索 |
| `cat_write_diary` | 猫娘自己写日记（单独存放；`mode=append` 追加，`mode=replace` 整篇重写，绝不修改每日日记） |
| `cat_read_diary` | 读取猫娘自己的日记（`date` 留空为今天） |
| `cat_list_days` | 列出猫娘写过日记的日期 |
| `capture` | 手动补记一句话（测试或记下不在聊天里的内容） |
| `toggle_memory` | 按日期+序号收藏/取消收藏一条对话（`date`、`index`、`on`） |
| `memories` | 列出所有收藏（回忆录） |
| `select_day` | 把面板切换到指定日期 |
| `capsule_write` | 封存一枚时间胶囊（`text`、`unlock_date`） |
| `capsule_list` | 列出时间胶囊；未到期的只回日期与字数（`limit`、`include_locked`） |
| `capsule_read` | 打开已到期的胶囊（按 `id` 或 `date`） |

### 静态浏览页（`static/index.html`）

插件管理器里点插件卡片上的「打开界面」，或直接访问：

```
http://127.0.0.1:48916/plugin/neko_daily_chronicle/ui/
```

页面里能按日期翻看**带序号的完整对话**（`#12 ｜ 16:59 ｜ 哥哥 ｜ …`）、看章节与日记、
搜索对话原文，并直接触发「立即补录 / 生成今天的章节 / 生成所选日期」。
它走插件的运行时入口（`/runs`），不依赖面板运行时；点任意一行会把序号复制到剪贴板，
并提示"可以对猫娘说：把 2026-10-06 的第 12 条念给我听"。插件没启动时页面会直接这么告诉你。

### 序号（`#编号`）——让猫娘调取记录的钥匙

每条记录都有"**当天第几条**"的序号：按时间排序、从 1 开始，回填更早的历史会让当天的号顺延。
章节正文、浏览页表格、检索结果、给猫娘的工具**用的是同一套号**，所以你在章节里看到 `#12`
就能直接让她取第 12 条。

### LLM 工具（让角色自己能翻记录）

| 工具 | 作用 |
| --- | --- |
| `chronicle_read_diary` | 读取猫娘自己的日记（对话里说到「日记」都指这一篇；也支持查某天） |
| `chronicle_love_diary` | 「爱妻日记」专用触发通道：读取某天自动生成的每日日记 |
| `chronicle_list_days` | 列出有记录的日期，用来挑要回忆哪一天 |
| `chronicle_note` | 把一件值得记住的事补记进当天记录，之后会出现在章节里 |
| `chronicle_recent` | 最近几条对话原文（"刚才我们聊了什么？"） |
| `chronicle_get_turn` | 按序号取某一条原文（"把今天第 12 条念给我听"） |
| `chronicle_read_day` | 读某天的对话区间（"把今天上午的对话读一遍"） |
| `chronicle_search_chat` | 在对话原文里搜关键词（"我说过油菜花吗？"） |
| `chronicle_cat_write_diary` | 猫娘把想记的事写进她自己那篇日记（追加或整篇重写） |
| `chronicle_read_diary` | 读取猫娘自己写的日记（对话里「日记」统一指向它） |

这几个工具都带**序号 + 时间 + 说话人**，且单个结果有长度上限，不会一次塞爆模型上下文。

### 回忆录（收藏）

浏览页的「对话记录」表格里，每行的**序号前有一个勾选框**：勾选 = 收藏进回忆录，
取消勾选 = 移出。收藏清单存到 ``data/memories.json``（内部状态，不生成可读文档）。
面板里也有一块「回忆录」卡片，直接列出收藏，点某条会跳到那天的对话并高亮它。
想让她把某条收藏念给你听，直接说"把 2026-10-06 第 12 条念给我听"（走 ``chronicle_get_turn``）。

### 日记（第一人称 + 立即生成按钮）

- **本地日记现在是猫娘第一人称**：以"我今天……"的口吻记当天发生的事与心情，
  字数限制在 ``diary_word_limit``（默认 400）内。
- 生成逻辑（路线乙）：能连到你的**个人 API** 就用模型写，否则退回本地第一人称。
- **"生成章节"不再顺带写日记**：章节按钮只生成章节；每章有独立的**「立即生成日记」**按钮。
  不点的话，过 0 点后的自动结算（``finalize_tick``）会先整理昨天的备忘录，再补上前一天的每日日记。
- **三篇各归各的**：备忘录、每日日记、猫娘自己的日记分开存放。对话里提到「日记」
  一律指猫娘自己那篇（``chronicle_read_diary`` / ``chronicle_cat_write_diary``）；
  想看自动生成的每日日记，发送「爱妻日记」+日期，走 ``chronicle_love_diary`` 专用通道。

---

## 配置

配置段是 `[chronicle]`。默认值写在 `plugin.toml`，首次运行模板是
`config.example.toml`，用户实际使用的是
`<用户数据根目录>/plugins/neko_daily_chronicle/config/plugin.toml`。
配置面板的字段说明由同目录的 `config.schema.json` 提供。

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `raw_dir` / `chapters_dir` / `diary_dir` | `raw` / `chapters` / `diary` | 存档子目录 |
| `scene_gap_minutes` | `30` | 超过该间隔另起场景；`0` = 整天不切分 |
| `chapter_title_format` | `第 {index} 章 · {date}` | 章节标题模板 |
| `include_system` | `false` | 是否把系统消息也写进章节 |
| `max_turn_chars` | `4000` | 单条消息在章节里最多保留多少字 |
| `diary_engine` | `auto` | `auto` / `local` / `llm` |
| `diary_style` | `first_person` | `first_person` / `third_person` / `record` |
| `diary_language` | `zh-CN` | `zh-CN` / `en` |
| `keyword_top_n` / `highlight_top_n` | `8` / `5` | 本地摘要的关键词与片段数量 |
| `llm_max_chars` | `24000` | 提示词里最多放多少字的原文（超出保留头尾） |
| `llm_timeout_seconds` / `llm_temperature` | `60` / `0.4` | 模型请求参数 |
| 模型端点 / API Key | — | 统一在浏览页「模型配置」按钮里维护，不再作为配置项重复出现 |
| `capture_all_sources` | `false` | 是否连系统/工具消息一起收 |
| `poll_memory_limit` / `poll_conversation_limit` | `50` / `200` | 每次向总线要多少条 |
| `live_refresh_seconds` | `300` | 多久刷新"今天"的章节 |
| `compile_on_startup` / `startup_backfill_days` | `true` / `3` | 启动时结算与回补 |
| `finalize_enabled` | `true` | 过 0 点后台自动结算（先整理昨日备忘录，再写前一天的每日日记；不发聊天提示） |
| `master_name` / `catgirl_name` | `主人` / `猫娘` | 章节与日记里的显示名 |
| `diary_word_limit` | `400` | 日记正文字数上限（本地第一人称日记按此截断） |
| `diary_source` | `memor` | 日记正文来源：`memor`（备忘录为主、对话为辅）/ `transcript`（只用对话） |
| `memo_append_mode` | `tail` | 备忘录抓取：`tail`（末段锚点只追新增）/ `line` / `append` |
| `search_default_days` | `7` | 猫娘「我说过…吗」默认往回查几天（另有 `search_default_limit` / `search_snippet_limit` / `search_include_diary`） |
| `cat_diary_enabled` | `true` | 是否允许猫娘自己写日记（工具 `chronicle_cat_write_diary` / `chronicle_read_diary` / `chronicle_search_diary` / `chronicle_diary_stats` / `chronicle_diary_throwback`） |
| `cat_diary_dir` | `neko_diary` | 猫娘日记存放目录：`<日期>.md` 是阅读视图，`.entries/<日期>.json` 是条目化数据源（每段一条，带 id/时间/心情/标签/可见性，可单独修改或删除）；旧版仅有 Markdown 时首次读取会自动迁移 |
| `cat_diary_default_mood` / `cat_diary_default_visibility` | `neutral` / `public` | 猫娘写日记时未指定心情/可见性所用的默认值 |
| `cat_diary_private_readable` | `true` | 浏览页与检索是否能看到猫娘标了「仅自己可见」的私密日记（猫娘自己始终读得到） |
| `redact` | `[]` | 写盘前替换成 `＊＊＊` 的字符串列表 |

非法值不会让插件失效：解析失败的项回落到默认值，并在日志与 `status` 入口里列出
`config_warnings`。

> 定时器间隔（采集 60 秒、结算 10 分钟）在装饰器求值时固定，因此没有做成配置项；
> 配置只控制"要不要做"（例如 `finalize_enabled`）和做事的节奏阈值。

---

## 关于日记使用的模型

N.E.K.O 的插件 SDK **没有**"让宿主模型帮我生成一段文字"的接口 ——
`@llm_tool` 是把插件能力注册给模型调用，方向相反。因此插件自己直连模型，
沿用官方仓库里 `forever_companion` 验证过的做法：读宿主 `core_config.json`
的模型槽位（`summaryModel*` → `agentModel*` → `conversationModel*` …），
用标准库 `urllib` 请求 OpenAI 兼容的 `/chat/completions`，零第三方依赖。

需要注意的边界：宿主若走 `coreApi = "free"`（lanlan.tech 免费代理），该代理有
服务端客户端校验，第三方直连会被拒绝。插件在这种情况下**明确拒绝复用该路由**，
直接退回本地摘要并说明原因，而不是让它静默失败。想用真日记，就在浏览页的
「模型配置」按钮里选一个可用模型（端点与密钥都在那里统一维护）。

日志与面板里永远不出现 API Key，只报告"是否已配置"。

---

## 测试与自检

```bash
# 单元测试 + 端到端冒烟测试（182 个，全部只用标准库）
python -m unittest discover -s tests -t . -v

# i18n key 与代码引用是否对得上
python tools/check_i18n.py

# 清单/入口/资源自检（等价于插件管理器"校验"按钮关心的那几件事）
python tools/check_plugin.py

# 用宿主自己的打包接口生成安装包（需要 N.E.K.O 正在运行）
python tools/package_plugin.py

# 重新生成 examples/ 下的示例章节与日记
python tools/make_demo.py
```

测试分三层：

* `tests/test_chronicle_core.py` —— 纯逻辑：文本归一化、去重、配置解析、
  章节编排、日记分析与解析模型回复、消息形态归一化；
* `tests/test_store_and_llm.py` —— 存储层（追加/去重/检索/目录页）与
  模型端点解析（含免费路由拒绝、具名服务商、显式端点）；
* `tests/test_entry_smoke.py` —— 用假的 SDK 与假的宿主上下文把插件入口整条链路跑通：
  `startup`、`@message` 采集、总线补录与去重、章节/日记生成、模型失败降级、
  脱敏、全部运行时入口、LLM 工具、面板上下文、结算与通知只发一次、配置热更新。

---

## 隐私与可见性

* 插件读取宿主的 `bus.conversations` 与 `bus.memory` 来补录对话。按官方文档说明，
  这两条总线对**所有已启用的插件**可读，因此安装本插件等于把这些内容对本地插件
  可见；本插件只在你的机器上把它们写成 Markdown/JSONL 文件，不做任何上传。
* `redact` 里的字符串在**写盘之前**就被替换，因此原始记录里也不会留下它们。
* 日记正文由你配置的模型端点生成；这部分内容会发送给该服务商，和你在 N.E.K.O
  里正常聊天时发给模型的内容同源。

---

## 验证状态与已知限制

### 面板按钮报 `UI action '…' is not a plugin entry`

这是**宿主在"插件没运行"时的固定回答**，不是插件代码错：点按钮的那一刻插件被停止，
或正在重载/启动，宿主的界面桥查不到该插件的任何入口，于是返回
`code=PLUGIN_UI_ACTION_NOT_FOUND, reason=no_plugin_entries`（HTTP 404）。
内置插件在重载窗口里同样会这样（日志里 2026-09-14 就有一条 `get_panel_background`）。

判断与恢复：看插件管理器里插件卡片是不是「运行中」；面板上的按钮现在**只在就绪时出现**，
并带一个「刷新面板」。宿主日志（`<用户数据根>\logs\N.E.K.O_Plugin_*.log`）里的
`Hosted UI action rejected: … reason=no_plugin_entries` 一行会写明是哪个动作被拒。

### 已经在真实 N.E.K.O 里跑过（0.9.0.1，0.2.0）

* 插件在宿主里加载并运行：宿主日志收集到 19 个运行时入口、2 个定时器
  （`harvest_tick` 60s / `finalize_tick` 600s）、`capture_chat` 消息处理器、
  7 个 LLM 工具（`chronicle_read_diary` / `chronicle_list_days` / `chronicle_note` /
  `chronicle_recent` / `chronicle_get_turn` / `chronicle_read_day` / `chronicle_search_chat`）
  与 Hosted UI context `dashboard`；
* 面板、说明页与**静态浏览页**都被宿主接受：`ui/panel.tsx` 编译通过，`warnings` 为空，
  `ui-info` 报告 `has_ui: true`、`ui_path: /plugin/neko_daily_chronicle/ui/`（该地址返回 200，
  含勾选框与回忆录页签）；
* 作者已改为「随遇」、版本 0.2.0（打包 `payload_hash_verified=True`）；
* 真实采集已经工作：`raw/2026-10-06.jsonl` 里双方对话都在（抓到过 267 句），
  当天章节自动生成到 `chapters/2026-10-06.md`，编号与读取接口口径一致；
* **回忆录**：`toggle_memory`/`memories` 入口在真机验证通过（收藏/取消/越界报错）；
* **立即生成日记**：`generate_diary` 生成 356 字的猫娘第一人称日记（本地引擎、≤400 字）；
* 182 个测试在本地 Python 3.12 下全部通过（含用替身 SDK 跑通整条入口链路的冒烟测试）。

### 消息总线的角色信息（重要）

**实测结论：消息总线只给"用户那一半"，角色的回复只在宿主自己的对话存储里。**
`bus.memory` 只有 `user_message`；`bus.conversations` 只回一份不更新的快照；
`bus.messages` 为空、`@message(source="chat")` 也不见触发。所以插件默认还会读

```
<用户数据根>\memory\<角色>\time_indexed.db     （表 time_indexed_original，human/ai 双方都在）
<用户数据根>\memory\<角色>\recent.json         （数据库读不到时的兜底）
```

用只读方式打开，读不到就静默跳过；可用 `read_host_memory_store = false` 关掉。
`bus.memory` 里的 `lanlan` 字段是**角色归属**，不是说话人，插件只把它记进 meta。

各来源的字段形态（真实抓到的）：

| 来源 | 记录形态 | 角色信息 |
| --- | --- | --- |
| `bus.memory` | `{"value": MemoryRecord(type='user_message', content=…)}`（对象在四层包装里） | 有（`user_message`） |
| `bus.conversations` | `{"type": "conversation_turn", "content": …, "timestamp": …}` | **没有**（只有 `conversation_turn`） |
| `host.store` | `{"type": "human"\|"ai", "data": {"content": [{"type":"text","text":…}]}}` | 有（`human` / `ai`） |

因此插件对"角色未知"的轮次按对话规律推断（详见 `chronicle/harvest.py`
的 `resolve_roles`）：

1. 紧跟在宿主环境提示后面的，是角色的回应；
2. 用户说完轮到角色、角色说完轮到用户；
3. 开头的未知项按后面第一个已知角色反向推断。

推断只改**角色标注**，原话一字不改；仍然无从判断的保持 `unknown`（章节里显示为
"记录"）。想确认某台机器上到底拿到了什么，触发 `inspect_capture` 入口即可看到
字段名与识别结果。

另外宿主会把**环境提示**（`======以下为环境提示======` 这类喂给模型的指令）
塞进对话流，插件按系统消息处理，默认不记录。

### 其他限制

* 开发模式下加载任意目录与本节说的"打包"是两条不同的路径：宿主的
  `/plugin-cli/build` 只认 `builtin` / `user` 两个插件根，所以本插件的源码
  在 `D:\N.E.K.O\.neko-plugin-installations\plugins\neko_daily_chronicle`
  留了一份（由 `tools/package_plugin.py` 同步）。
* 本地摘要的关键词是**启发式的**（没有分词器可用，采用"虚词切分 + n-gram + 完整片段
  加权"），偶尔会出现偏长的片段；想要准确的关键词请让日记走模型路径 ——
  模型会在回复开头自己给出 `关键词：` 一行。
* 跨天的长会话按**消息时间戳**归日（本地时区），"凌晨 4 点仍算前一天"这类需求
  目前没有实现。
* 定时器间隔（60s / 600s）在装饰器求值时固定，因此不是配置项；配置只控制
  "要不要做"与节奏阈值。

---

## 目录一览

```
neko_daily_chronicle/
├── plugin.toml                 # 插件清单（含 [chronicle] 默认配置）
├── config.example.toml         # 用户运行配置模板
├── config.schema.json          # 配置面板字段说明
├── __init__.py                 # 插件入口：生命周期、采集、定时器、LLM 工具、Hosted UI
├── routers/                    # 按功能域拆分的 @plugin_entry
│   ├── config.py               # 状态、模型目录、插件配置
│   ├── chronicle.py            # 章节、每日日记、备忘录、回忆录
│   ├── cat_diary.py            # 猫娘自己的日记
│   ├── images.py               # 章节图集
│   ├── search.py               # 对话原文检索与补记
│   └── capsule.py              # 时间胶囊（封存/列出/到期读取）
├── chronicle/                  # 纯逻辑核心（只依赖标准库，可独立测试）
│   ├── models.py               # Turn 模型、时间与文本工具、去重键
│   ├── config.py               # [chronicle] 配置解析与校验
│   ├── chapter.py              # 章节编排（分场景、标时间、标说话人）
│   ├── diary.py                # 日记：本地第一人称摘要 + 提示词 + 解析模型回复
│   ├── harvest.py              # 宿主消息形态归一化（反复剥包装 + 跨来源去重）
│   ├── host_store.py           # 读宿主自己的对话存储（双方对话都在这里）
│   ├── memory.py               # 收藏清单（data/memories.json）
│   ├── reader.py               # 序号、按号取用、跨天检索（章节/界面/工具共用）
│   ├── store.py                # JSONL 追加、去重、原子写、检索、目录页
│   └── llm.py                  # 宿主配置定位 + 模型端点解析 + 直连
├── ui/panel.tsx                # Hosted UI 面板
├── static/index.html           # 静态浏览页（/plugin/neko_daily_chronicle/ui/）
├── docs/quickstart.md          # 面板里的「使用说明」
├── i18n/zh-CN.json, en.json    # 界面文案
├── dist/                       # 打包产物（由 tools/package_plugin.py 生成；dist/archive/ 存旧版本备份）
├── tests/                      # 182 个测试
├── tools/                      # check_i18n / check_plugin / make_demo / package_plugin
└── examples/                   # 由真实代码生成的示例章节与日记
```

## 许可

MIT
