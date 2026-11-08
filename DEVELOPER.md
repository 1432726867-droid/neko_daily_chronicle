# 对话编年史 · 开发文档（DEVELOPER.md）

> 面向**接续开发**本插件的开发者。本文档是开发者的单一入口：
> 功能、用法、全部调用接口、代码结构、开发流程、以及**变更记录**。
>
> **强制约定：每次修改本插件，都必须在本文件末尾「变更记录」追加一行**，
> 写明改了什么、为什么、影响哪些文件。别人靠这份记录才能接得上手。

- 插件 ID / 目录名 / entry 包名：`neko_daily_chronicle`（三者必须一致）
- 版本：`0.2.0`（改功能/改接口时递增）
- 宿主：N.E.K.O（Electron 外壳 + 冻结的 Python 服务 `projectneko_server.exe`）
- 插件服务器：默认 `http://127.0.0.1:48916`
- 运行数据目录：`<用户数据根>/plugins/neko_daily_chronicle/data/`
- 运行配置：`<用户数据根>/plugins/neko_daily_chronicle/config/plugin.toml`

---

## 1. 这个插件做什么

把每天你和猫娘/角色在 N.E.K.O 里的对话**完整留下来**，并整理成几样东西：

| 产出 | 位置 | 说明 |
| --- | --- | --- |
| 原始记录 | `data/raw/YYYY-MM-DD.jsonl` | 每条对话一行 JSON，**只追加、不覆盖**，是唯一事实来源 |
| 章节 | `data/chapters/YYYY-MM-DD.md` | 小说式排版：分场景、标时间、标说话人，**原话不改写** |
| 自动日记 | `data/diary/YYYY-MM-DD.md` | 每章一篇，优先由模型写，否则本地第一人称摘要 |
| 每日备忘录 | `data/chapters/备忘录-*.md` | 抓宿主 recent.json 的「先前对话的备忘录」，只追加新增段 |
| 猫娘日记 | `data/neko_diary/YYYY-MM-DD.md` | 猫娘**自己**写，单独存放、只追加、不覆盖，绝不改自动日记 |

核心设计取向：**原始记录先行**。章节、日记都是原始记录的派生，删掉可随时重算；
换模型、改排版、修 bug 都不会丢已记下的对话。

---

## 2. 技术栈与 SDK

- 基于 `NekoPluginBase`，用装饰器：`@neko_plugin`、`@lifecycle`、`@message`、
  `@timer_interval`、`@plugin_entry`、`@ui.context` / `@ui.action`、`@llm_tool`。
- Hosted UI 用 TSX：`ui/panel.tsx`，只从 `@neko/plugin-ui` 导入组件。
- **零第三方依赖**：模型直连用标准库 `urllib`；数据库是 SQLite（宿主 `self.db` 或用文件）。
- 采集依赖宿主：`bus.memory`（用户消息）、`bus.conversations`、
  `host.store`（`<用户数据根>/memory/<角色>/time_indexed.db`，双方对话都在这里）。
- SDK 源码被冻结在 `projectneko_server.exe` 里，API 靠官方文档 + 真实插件源码核对，
  不要凭记忆猜接口。

---

## 3. 数据流

```
@message(source="chat")  ─┐
                          ├─→ 归一化 + 指纹去重 ─→ raw/YYYY-MM-DD.jsonl（只追加）
@timer_interval(60s) 轮询 ─┘
   bus.memory / bus.conversations / host.store        │
                                                       ▼
                              章节生成（纯函数，可随时重算）→ chapters/*.md
                                                        │
                                                        ▼
                   模型可用？ ──是──→ 提示词 → /chat/completions → 自动日记
                              └─否──→ 本地统计摘要（关键词/心情/片段）→ 自动日记
```

- 两条采集来源按"精确指纹 + 时间桶指纹"去重，既不漏也不重。
- 角色回复只在宿主对话存储里，总线只给用户一半；插件按"一问一答交替"推断角色。
- 宿主会把环境提示（`======以下为环境提示======`）塞进流，按系统消息滤掉。
- 所有落盘走同一把锁，写文件先写 `.tmp` 再 `os.replace`，避免半截文件。

---

## 4. 使用方式（给用户的速览）

- 启动后每 60s 补录、每 5min 刷新"今天"章节、每 30min 抓一次备忘录、跨零点结算昨天。
- 面板：生成今天 / 生成所选日期 / 立即生成日记 / 重算全部日期 / 立即补录 / 立即抓取备忘录。
- 浏览页：`/plugin/neko_daily_chronicle/ui/`，按日期翻看带序号的完整对话、看章节/日记、
  搜索原文、收藏回忆录、**点某行复制序号短语**。
- 让猫娘调取记录靠**序号**（`#12` = 当天第 12 条）：`把 10月7号第13条念给我听`。

---

## 5. 调用接口（全部入口）

### 5.1 运行时入口（Agent / 插件管理器 / 浏览页可调）

| 入口 | 作用 | 关键参数 |
| --- | --- | --- |
| `status` | 天数/章节/日记/今天句数/模型状态/数据目录/复制短语配置 | — |
| `harvest_now` | 立即从总线补录一次 | — |
| `inspect_capture` | 排障：打印总线字段名与角色识别结果（不写入） | `sample` |
| `build_day` | 生成指定日期的章节与日记 | `date`,`force`,`with_llm`,`with_diary` |
| `build_today` | 生成今天的章节（可选带日记） | `with_diary` |
| `generate_diary` | 立即生成某天的自动日记 | `date` |
| `build_all` | 按原始记录重算全部日期 | `with_diary`,`limit` |
| `list_days` | 列出有记录的日期及章节/日记/猫娘日记状态 | `limit` |
| `read_chapter` / `read_diary` | 读某天正文（默认只回元信息） | `date` |
| `read_day` | 读某天的对话原文（带序号，分页） | `date`,`start`,`count` |
| `get_turn` | 按序号取一条原文 | `index`,`date` |
| `recent` | 最近几条原文 | `count`,`date` |
| `search_chat` | 在对话原文里检索 | `query`,`limit`,`days` |
| `search` | 在章节与日记里全文检索 | `query`,`limit` |
| `cat_write_diary` | 猫娘自己写一篇日记（追加、不覆盖；可带心情/标签/可见性） | `date`,`text`,`mood`,`tags`,`visibility` |
| `cat_read_diary` | 读猫娘自己的日记（含「仅自己可见」的私密内容） | `date` |
| `cat_list_days` | 列出猫娘写过日记的日期 | — |
| `list_cat_diaries` | 一次返回猫娘自己写的全部日记（含私密），供浏览页「猫娘日记」 | `limit` |
| `capture` | 手动补记一句话 | `text`,`role`,`speaker` |
| `toggle_memory` | 按日期+序号收藏/取消收藏 | `date`,`index`,`on` |
| `memories` | 列出收藏（回忆录） | — |
| `save_memo` / `save_title` / `save_diary` | 保存某天的备忘录/章节标题/自动日记 | `date`,`text` |
| `fetch_memo_now` | 立即抓取备忘录 | — |
| `memo_state` | 读某天备忘录+标题+自动日记 | `date` |
| `select_day` | 面板切换到指定日期 | `date` |
| `capsule_write` | 封存一枚时间胶囊 | `text`,`unlock_date` |
| `capsule_list` | 列出时间胶囊（未到期只回日期/字数；带 `master_count` / `catgirl_count`） | `limit`,`include_locked`,`author` |
| `capsule_nudge` | 猫娘一枚都没写时，把补写提醒递给她（`respond`），已写过则不打扰 | `unlock_date` |
| `capsule_read` | 打开已到期胶囊（按 id 或日期） | `id`,`date` |

> 日期参数 `date` 接受 `YYYY-MM-DD` / `YYYY/MM/DD` / `YYYY.MM.DD` / `YYYYMMDD`，
> 也接受自然中文：`今天/昨天/前天/明天/后天`、`10月7号`、`十月七日`、`2026年10月7日`、
> `二〇二六年十月七日`（年份月日里的中文数字也认）。
> 序号 `index` 接受阿拉伯或中文数字（`13` / `第十三` / `13条`）。

### 5.2 LLM 工具（让角色自己能翻记录）

| 工具 | 作用 |
| --- | --- |
| `chronicle_read_diary` | 读某天日记（无日记退化为章节，返回 `kind` 标明） |
| `chronicle_list_days` | 列出有记录的日期 |
| `chronicle_note` | 补记一件值得记住的事进当天记录 |
| `chronicle_recent` | 最近几条原文 |
| `chronicle_get_turn` | 按序号取一条原文 |
| `chronicle_read_day` | 读某天对话区间 |
| `chronicle_search_chat` | 在对话原文里搜关键词 |
| `chronicle_cat_write_diary` | 猫娘把想记的事写进她那篇日记（追加或整篇重写） |
| `chronicle_read_diary` | 读猫娘自己写的日记（对话里「日记」统一指向它） |
| `chronicle_love_diary` | 「爱妻日记」+日期的专用通道，读自动生成的每日日记 |
| `chronicle_write_capsule` | 猫娘以自己口吻封存未来某天才能打开的话 |
| `chronicle_list_capsules` | 列出时间胶囊（未到期不返回正文） |
| `chronicle_read_capsule` | 打开已到期胶囊；未到期返回剩余天数 |

### 5.3 生命周期 / 定时器 / 消息

| 类型 | id | 触发 | 说明 |
| --- | --- | --- | --- |
| `@lifecycle` | `startup` / `shutdown` / `config_change` | 生命周期 | 读配置、建 store、补录、结算 |
| `@timer_interval` | `harvest_tick` (60s) | 定时 | 从总线补录 + 刷新今天章节 |
| `@timer_interval` | `finalize_tick` (600s) | 定时 | 结算已结束日期的章节+日记+提示 |
| `@timer_interval` | `memo_tick` (1800s) | 定时 | 抓宿主 system 备忘录 |
| `@timer_interval` | `capsule_tick` (3600s) | 定时 | 每天设定时刻后检查到期胶囊，只提醒猫娘一次 |
| `@message` | `capture_chat` (source=chat) | 聊天消息 | 实时接住每条消息（同步、不抛异常） |

> 定时器间隔在**装饰器求值时固定**，因此是写死常量，不做配置；配置只控制
> "要不要做"（如 `finalize_enabled`）与节奏阈值。

### 5.4 Hosted UI

- `@ui.context(id="dashboard")` → 面板 `props.state`：天数、日期列表、章节/自动日记/
  猫娘日记正文、备忘录、模型状态（含 `probed` / `usable`）、文件位置、回忆录、
  时间胶囊（未到期不带正文）。
- `@ui.action`：`select_day`、`check_llm`、`harvest_now`、`build_day`、`build_today`、
  `build_all`、`generate_diary`、`toggle_memory`、`save_memo`、`save_title`、
  `save_diary`、`cat_write_diary`、`fetch_memo_now`、`capsule_write`。
- 面板/浏览页的模型状态**以真实探测结果为准**：`probed=true` → 绿「可用」；
  `probed=false` → 红「不可用（原因）」；未探测 → 橙「待检查」。不要用
  `available`（那只代表"配置了端点"）当"真能用"。

---

## 6. 配置（`[chronicle]` 段）

完整清单见 `config.example.toml` 与 `config.schema.json`。常用项：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `diary_engine` | `auto` | `auto`/`llm` 用模型；`local` 只本地摘要 |
| `diary_style` / `diary_language` | `first_person` / `zh-CN` | 日记风格与语言 |
| `diary_source` | `memor` | 日记正文来源：`memor` / `transcript` |
| `memo_append_mode` | `tail` | 备忘录抓取：`tail` / `line` / `append` |
| 模型端点 / API Key | — | 统一在浏览页「模型配置」按钮维护，不再作为插件配置项 |
| `llm_timeout_seconds` / `llm_temperature` | `60` / `0.4` | 模型请求超时与温度（保留为配置项） |
| `cat_diary_enabled` / `cat_diary_dir` / `cat_diary_word_limit` | `true` / `neko_diary` / `500` | 猫娘日记 |
| `cat_diary_default_visibility` / `cat_diary_default_mood` | `public` / `neutral` | 猫娘日记默认可见性与心情（`private`=仅自己可见，猫娘自己仍读得到） |
| `copy_phrase_prefix` | `把` | 浏览页复制短语里**日期前**的词 |
| `copy_phrase_mid` | `的第` | 日期与序号之间的词 |
| `copy_phrase_index_unit` | `条` | 序号后的单位 |
| `copy_phrase_suffix` | `念给我听` | 复制短语末尾 |
| `scene_gap_minutes` | `30` | 间隔超过则另起场景；`0` 整天不切分 |
| `master_name` / `catgirl_name` | `主人` / `猫娘` | 章节/日记里的显示名 |
| `redact` | `[]` | 写盘前替换成 `＊＊＊` 的字符串 |
| `search_default_days` / `search_default_limit` / `search_snippet_limit` | `7`/`20`/`120` | 检索默认 |
| `capture_all_sources` | `false` | 是否连系统/工具消息一起收 |
| `read_host_memory_store` | `true` | 是否读宿主对话存储（双方对话） |

复制短语拼法：`{copy_phrase_prefix} {日期} {copy_phrase_mid} {序号} {copy_phrase_index_unit} {copy_phrase_suffix}`
默认 → `把 2026-10-07 的第 13 条念给我听`。四段都可在运行配置里自定义。

非法配置不会让插件失效：解析失败回落到默认值，并在日志与 `status` 的
`config_warnings` 里列出。

---

## 7. 代码结构（接续开发前先读）

```
neko_daily_chronicle/
├── plugin.toml              # 清单 + [chronicle] 默认配置
├── config.example.toml      # 用户运行配置模板
├── config.schema.json       # 配置面板字段说明
├── __init__.py              # 插件入口：生命周期、采集、定时器、LLM 工具、Hosted UI
├── routers/                 # 按功能域拆分的 @plugin_entry（通过 self.main_plugin 访问状态）
│   ├── config.py            # 状态、模型目录、插件配置
│   ├── chronicle.py         # 章节、每日日记、备忘录、回忆录
│   ├── cat_diary.py         # 猫娘自己的日记
│   ├── images.py            # 章节图集
│   ├── search.py            # 对话原文检索与补记
│   └── capsule.py           # 时间胶囊（封存/列出/到期读取）
├── chronicle/               # 纯逻辑核心（只依赖标准库，可独立测试）
│   ├── models.py            # Turn 模型、时间/文本工具、去重键
│   ├── config.py            # [chronicle] 配置解析与校验（含 copy_phrase_*）
│   ├── chapter.py           # 章节编排（分场景、标时间、标说话人）
│   ├── diary.py             # 自动日记：本地摘要 + 提示词 + 解析模型回复
│   ├── harvest.py           # 宿主消息形态归一化（剥包装 + 跨来源去重 + 角色推断）
│   ├── host_store.py        # 读宿主自己的对话存储（双方对话）
│   ├── memory.py            # 收藏清单（data/memories.json）
│   ├── reader.py            # 序号、按号取用、跨天检索（章节/界面/工具共用）
│   ├── store.py             # JSONL 追加、去重、原子写、检索、目录页、猫娘日记
│   └── llm.py               # 宿主配置定位 + 模型端点解析 + 直连 /chat/completions
├── ui/panel.tsx             # Hosted UI 面板（模型状态诚实显示、日记切换、胶囊按钮）
├── static/index.html        # 静态浏览页（复制序号短语可配置、模型状态徽标）
├── docs/quickstart.md       # 面板里的「使用说明」
├── i18n/zh-CN.json, en.json # 界面文案
├── tests/                   # 182 个测试（纯逻辑 + 存储 + 入口冒烟）
├── tools/                   # check_plugin / check_i18n / make_demo / package_plugin
├── dist/                    # 打包产物（tools/package_plugin.py 生成）
└── examples/                # 真实代码生成的示例章节/日记
```

**接续开发提示**：
- 业务逻辑尽量放 `chronicle/` 纯函数模块，`__init__.py` 只做装配和装饰器绑定。
- 新入口按功能域加在 `routers/*.py`：入口里用 `self.main_plugin.xxx` 访问插件状态与内部方法，入口 id 全插件唯一。
- 序号（`#编号`）由 `reader.numbered_turns` 统一定，章节/浏览页/工具用同一套，
  改编号逻辑会影响所有展示，务必跑 `tests/`。
- 配置默认值、解析、`as_public_dict` 三处要一起改（`chronicle/config.py`）。
- 面板新增 i18n key 要在 `i18n/zh-CN.json` 和 `en.json` 都加（`check_i18n.py` 会查）。

---

## 8. 开发流程（每次改完必走）

```bash
# 1. 静态自检（清单/入口/资源/语法，等价于宿主「校验」）
python tools/check_plugin.py

# 2. i18n key 对齐
python tools/check_i18n.py

# 3. 单元测试 + 入口冒烟（182 个，纯标准库）
python -m unittest discover -s tests -t . -v

# 4. 在真机里重载（改完源码必须 reload 才生效）
python <skill>/scripts/host_api.py reload neko_daily_chronicle
python <skill>/scripts/host_api.py logs neko_daily_chronicle      # 看日志排障
python <skill>/scripts/host_api.py surfaces neko_daily_chronicle  # 看面板 warnings
python <skill>/scripts/host_api.py entries neko_daily_chronicle   # 看收集到的入口/工具
python <skill>/scripts/host_api.py trigger neko_daily_chronicle <entry> --args-file args.json

# 5. 需要交付时打 .neko-plugin 包（N.E.K.O 必须在运行）
python tools/package_plugin.py    # 同步源码 → 调宿主打包 → 校验哈希 → 复制到 dist/
```

> 打包只有一条正路：让宿主自己打包（`/plugin-cli/build` 只认宿主自己的插件根）。
> `.neko-plugin` 里的 `payload_hash` 只有宿主能算对，不要手搓 zip。
> 目录名 / 插件 ID / entry 包名必须一致。

**改完代码后，别忘了在本文件「变更记录」追加一行**——这是硬性约定。

---

## 9. 已知边界与注意事项（接续开发必读）

- **消息总线只给"用户那一半"**：`bus.memory` 只有 `user_message`；`bus.conversations`
  只回一份快照；角色回复要读宿主 `time_indexed.db`（`read_host_memory_store`）。
- **解包要反复剥**：`SdkRecord → .raw={"value": MemoryRecord} → MemoryRecord`，
  停在任何一层都会把 `content`/`type` 吞掉。对象转 dict 时合并内层 `.raw`。
- **宿主把环境提示塞进对话流**：按系统消息过滤，别当"角色说的话"。
- **模型 401 / 网络不通时不抛异常**：`llm.chat_completion` 返回 `(None, reason)`，
  调用方降级成本地摘要。`_build_day` 会把 `skipped=no_model` 和 `reason` 带进返回，
  UI 据此提示"日记未生成"。
- **模型状态显示要看 `probed` / `usable`**，不要只看 `available`（配置了端点≠真能用）。
- **定时器各自跑在独立线程**，落盘必须加锁；`@message` 处理器是同步的，异常不能冒泡。
- **`@plugin_entry` 处理器必须 `async def`**，返回 `Ok(...)` / `Err(SdkError(...))`，
  不要抛异常。`@llm_tool` 返回普通 dict，用 `*,` 强制 keyword-only。
- **配置只控制"要不要做"**，定时器间隔是写死常量。
- 沙箱里 `tempfile.mkdtemp()` 建不了目录（WinError 5），测试用普通 `mkdir`。

---

## 10. 变更记录（CHANGELOG）

> **规则：每改一次插件，都必须在此表顶部追加一行。**
> 格式：`日期 | 版本 | 改动摘要 | 涉及文件`。这能让后来者快速看出每个版本改了什么。

| 日期 | 版本 | 改动摘要 | 涉及文件 |
| --- | --- | --- | --- |
| 2026-11-02 | 0.2.0 | **新增**：`capsule_nudge`（面板按钮「提醒猫娘补写」）——检查 `catgirl_count`，为 0 时把补写提醒用 `_push_to_catgirl(..., respond=True)` 递给她并让她开口回应，已写过则不打扰；`_push_to_catgirl` 新增 `respond` 参数（`ai_behavior="respond"`）。注意：宿主 Agent 的插件任务闸门只认**主人消息**，插件推送的系统通知 `external_intent=0` 不会触发她执行插件任务，所以推送只能让她「知道」，真正落盘仍需主人的指令触发 | `routers/capsule.py`、`__init__.py`、`i18n/*.json`、`tests/test_entry_smoke.py` |
| 2026-11-02 | 0.2.0 | **修复**：区分「主人写的」和「猫娘写的」胶囊——`capsule_list` 与 `chronicle_list_capsules` 新增 `author` 过滤与 `master_count` / `catgirl_count`（并把 `catgirl_count` 放在 `llm_result_fields` 首位，避免宿主摘要只报总数被误读成「她写好了」）；面板与浏览页按作者分组显示；工具描述明确「主人问『你写了吗』只能看 catgirl 的条目」 | `chronicle/capsule.py`、`routers/capsule.py`、`__init__.py`、`ui/panel.tsx`、`static/index.html`、`i18n/*.json`、`tests/test_capsule.py`、`tests/test_entry_smoke.py` |
| 2026-11-02 | 0.2.0 | **新增**：时间胶囊（`capsule_write` / `capsule_list` / `capsule_read`）——一枚一个 JSON，到期前只返回日期与字数、正文绝不下发；到期后主人在面板/浏览页、猫娘经 `chronicle_write_capsule` / `chronicle_list_capsules` / `chronicle_read_capsule` 可读写；新增 `capsule_tick` 到期提醒（每小时检查、默认 9 点后、每枚只提醒猫娘一次），配置 `capsule_dir` / `capsule_notify_enabled` / `capsule_notify_hour` | `chronicle/capsule.py`、`routers/capsule.py`、`__init__.py`、`chronicle/config.py`、`chronicle/store.py`、`ui/panel.tsx`、`static/index.html`、`plugin.toml`、`config.example.toml`、`config.schema.json`、`i18n/*.json`、`tests/test_capsule.py`、`tests/test_entry_smoke.py` |
| 2026-11-02 | 0.2.0 | **重构**：47 个 `@plugin_entry` 从 `__init__.py` 拆进 `routers/`（config/chronicle/cat_diary/images/search），主类只保留生命周期、采集、定时器、LLM 工具与 Hosted UI；入口 id 与返回结构不变。**修复**：浏览页与面板的日期列表不再限制 90/60 天，7 月等旧记录重新可见；删除 9 个未引用的 panel i18n key；`_day_cards` 对面板跳过图片计数与心情解析，图片数量按目录 mtime 缓存 | `__init__.py`、`routers/*`、`static/index.html`、`i18n/*.json`、`tools/check_i18n.py`、`tests/test_entry_smoke.py`、`README.md`、`DEVELOPER.md` |
| 2026-10-16 | 0.2.0 | **安全清理**：把硬编码在 `profiles/default.toml` 里的模型端点/API Key 迁到用户运行配置 `<数据根>/plugins/neko_daily_chronicle/config/plugin.toml`（不再随 .neko-plugin 打包分发明文密钥）；**修 i18n**：补 `listModels`/`saveModels`/`fetchModels` 六个缺失 key、删 `panel.actions.unavailable`/`panel.llm.available`/`panel.llm.unavailable` 三个未用 key；**修 config 一致性**：`str_field` 对非法值也记入 `warnings`；**修 docstring**：`chronicle/diary.py` 模块说明改为如实描述"模型不可用时日记留空、不降级" | `profiles/default.toml`、`<数据根>/plugins/neko_daily_chronicle/config/plugin.toml`、`i18n/zh-CN.json`、`i18n/en.json`、`chronicle/config.py`、`chronicle/diary.py` |
| 2026-10-16 | 0.2.0 | **改进**：浏览页「📚 全部日记」标签页改为「🐱 猫娘日记」——入口由 `list_diaries` 改名为 `list_cat_diaries` 且**只返回猫娘自己写的日记**（与「📔 日记」里的自动日记区分开）；页签 emoji 修正为 🐱，`静态/index.html` 去掉自动日记的 `.badge.auto` | `__init__.py`、`static/index.html`、`i18n/*.json`、`DEVELOPER.md` |
| 2026-10-16 | 0.2.0 | **新增**：浏览页加入「📚 全部日记」标签页——新增 `list_diaries` 入口一次返回全部日记（自动日记 + 猫娘日记，含私密），页面按日期倒序渲染成卡片并支持**上下滚动**（`.diary-scroll`，max-height 72vh + overflow 滚动） | `__init__.py`、`static/index.html`、`i18n/*.json` |
| 2026-10-16 | 0.2.0 | **改进**：借 neko_diary 的写法给猫娘日记加上 **心情 / 标签 / 可见性**——`cat_write_diary` 入口与 `chronicle_cat_write_diary` 工具新增 `mood` / `tags` / `visibility` 参数（`private`=「仅自己可见」，但猫娘自己**仍读得到**，`chronicle_cat_read_diary` 不隐藏）；元信息以 `> ✎ HH:MM ｜ …` 追加进当天文件；新增配置 `cat_diary_default_visibility` / `cat_diary_default_mood` | `__init__.py`、`chronicle/diary.py`、`chronicle/store.py`、`chronicle/config.py`、`plugin.toml`、`config.example.toml`、`i18n/*.json`、`docs/quickstart.md` |
| 2026-10-16 | 0.2.0 | **改进**：把猫娘写日记的**口吻**写进功能——`cat_write_diary` 入口与 `chronicle_cat_write_diary` 工具的描述、`text` 字段说明均要求以猫娘第一人称、软萌口吻（自然地用「喵～」「的说」「哦～」等语气词，写给主人看）书写；`docs/quickstart.md` 同步 | `__init__.py`、`i18n/*.json`、`docs/quickstart.md` |
| 2026-10-14 | 0.2.0 | **改进**：让宿主 LLM 优先调用插件记录——强化 `chronicle_get_turn` / `chronicle_recent` / `chronicle_read_day` / `chronicle_search_chat` / `chronicle_read_diary` 的工具 `description`，明确"这是对话记录的权威来源，主人问第N条/念给我听/最近聊了什么/我说过吗时直接调用本工具，不要先查自己的记忆或宿主数据库" | `__init__.py` |
| 2026-10-14 | 0.2.0 | **新增**：复制短语四段可配置（`copy_phrase_*`），`status` 暴露 `copy_phrase`，浏览页复制处理器用配置片段拼短语；**修复**：`chronicle_read_diary` 无日记时退化为章节并返回 `kind` 区分（避免猫娘把章节当日记） | `chronicle/config.py`、`__init__.py`、`plugin.toml`、`config.example.toml`、`<用户数据根>/.../config/plugin.toml`、`static/index.html` |
| 2026-10-14 | 0.2.0 | **修复**：浏览页「点内容可复制序号」改用 `copyText()`（`navigator.clipboard` + `execCommand` 回退，iframe 也能复制），复制含日期+序号的完整短语并给「已复制」反馈 | `static/index.html` |
| 2026-10-14 | 0.2.0 | **修复**：模型状态诚实化——新增 `llm_probed` / `usable` / `probe_detail`，`_probe_llm` 记录真实探测结果；`generate_diary` 失败返回 `skipped=no_model` + `reason`；面板/浏览页按 `probed` 显示 绿「可用」/红「不可用」/橙「待检查」 | `__init__.py`、`ui/panel.tsx`、`static/index.html`、`i18n/*.json` |
| 2026-10-13 | 0.2.0 | **修复**：`get_turn`/`recent`/`read_day` 等入口支持自然中文日期（今天/昨天/前天、10月7号、十月七日、2026年10月7日、中文数字）与中文序号（第十三/13条）；新增 `_normalize_day_text` / `_chinese_to_int` | `__init__.py`、`i18n/*.json` |
| 2026-10-13 | 0.2.0 | **界面**：面板改造——动作按钮缩小为横排、新增「自动日记/猫娘日记」切换按钮、`_day_cards` 增加 `cat_diary` 标记、检查模型按钮旁加状态徽标 | `ui/panel.tsx`、`__init__.py`、`i18n/*.json` |
| 2026-10-13 | 0.2.0 | 整理并验证（续） | 全插件 |

> 注：以上为近期整理记录，更早的演进见 `README.md` 的「验证状态与已知限制」。
> **每次修改务必在此表顶部追加新行**，不要只改代码不记录。

---

## 11. 许可

MIT