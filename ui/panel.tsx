// 对话编年史 · Hosted UI 面板
//
// 借鉴了 neko_diary 的界面风格：圆角胶囊按钮、蓝色渐变选中态、紧凑排布。
// 新增「自动日记 / 猫娘日记」切换按钮；动作按钮改为小尺寸横排，不再占满整行；
// 「检查模型」按钮旁带一个醒目的可用/不可用状态徽标。
//
// 只使用官方 UI Kit 里已验证过的组件；业务逻辑全部留在 Python，
// 这里只负责把 @ui.context(id="dashboard") 的状态画出来，并把按钮
// 绑定到 @ui.action 暴露的入口上。
import {
  Page,
  Card,
  Grid,
  Stack,
  Text,
  Tip,
  Warning,
  StatCard,
  ActionButton,
  KeyValue,
  Divider,
} from "@neko/plugin-ui"
import type { HostedAction, PluginSurfaceProps } from "@neko/plugin-ui"
import { useLocalState } from "@neko/plugin-ui"

type DayCard = {
  date?: string
  turns?: number
  chapter?: boolean
  diary?: boolean
  cat_diary?: boolean
  mood?: string
  chapter_title?: string
}

type LlmEndpoint = {
  model?: string
  base_url?: string
  provider?: string
  source?: string
  has_key?: boolean
}

type LlmStatus = {
  available?: boolean
  reason?: string
  detail?: string
  endpoint?: LlmEndpoint | null
  config_path?: string
  probed?: boolean | null
  probe_reason?: string
  probe_detail?: string
  usable?: boolean
}

type ChronicleState = {
  ready?: boolean
  today?: string
  days?: DayCard[]
  days_count?: number
  chapters_count?: number
  diaries_count?: number
  today_turns?: number
  selected?: string
  selected_exists?: boolean
  chapter_text?: string
  diary_text?: string
  chapter_truncated?: boolean
  diary_truncated?: boolean
  cat_diary_text?: string
  cat_diary_exists?: boolean
  cat_diary_truncated?: boolean
  cat_diary_hidden?: boolean
  cat_diary_days?: string[]
  memo_text?: string
  memo_title?: string
  llm?: LlmStatus
  paths?: Record<string, string>
  ui_path?: string
  numbering_hint?: string
  memories_count?: number
  memories?: Array<{
    date?: string
    index?: number
    time?: string
    speaker?: string
    text?: string
  }>
  capsules?: Array<{
    id?: string
    author?: string
    author_name?: string
    created_day?: string
    unlock_day?: string
    locked?: boolean
    days_left?: number
    chars?: number
    text?: string
  }>
  capsules_due?: number
  config?: Record<string, any>
}

const DAYS_PAGE_SIZE = 4 // 基础可见行数
const VISIBLE_ROWS = DAYS_PAGE_SIZE + 3 // 视口可见行数（在原 5 行基础上再加 2 行 = 7 行）
const TITLE_MAX = 20 // 章节标题最多显示 20 个字
const ROW_FONT = 13 // 行文字字号(px)
const ROW_LINE = ROW_FONT * 2 // 行高 = 文字高度的 2 倍

// 猫娘粉按钮风格：圆角胶囊、粉色渐变选中态
const segWrap: React.CSSProperties = {
  display: "inline-flex",
  gap: "4px",
  padding: "3px",
  background: "rgba(255,140,170,0.12)",
  borderRadius: "999px",
  border: "1px solid rgba(255,140,170,0.30)",
  alignItems: "center",
}

const segBtn = (active: boolean): React.CSSProperties => ({
  border: "none",
  padding: "5px 14px",
  borderRadius: "999px",
  cursor: "pointer",
  fontSize: "12px",
  fontWeight: 600,
  fontFamily: "inherit",
  color: active ? "#fff" : "#a06a7c",
  background: active
    ? "linear-gradient(135deg,#ff9ebb 0%,#ff7fa5 100%)"
    : "transparent",
  boxShadow: active ? "0 2px 10px rgba(255,127,165,0.40)" : "none",
  transition: "all 0.25s ease",
  lineHeight: "1.2",
})

// 按日期列表：保持"每日一行"的表格样式，鼠标滚轮在固定高度容器内上下滚动浏览。
// 标题行（"按日期" + 共 N 天）横向排列；章节标题最多 20 字；"查看这天"按钮单行显示。
function DaysPager(props: {
  days: DayCard[]
  selected: string
  t: (key: string) => string
  selectDay?: HostedAction
}) {
  const { days, selected, t, selectDay } = props
  const total = days.length

  const truncate = (s: string) =>
    s.length > TITLE_MAX ? s.slice(0, TITLE_MAX) + "…" : s

  return (
    <Stack>
      {/* 标题行：按日期（左） + 共 N 天（右）横向排列 */}
      <Stack direction="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <Text color="primary">
          <strong>{t("panel.days.title")}</strong>
        </Text>
        <Text color="muted">
          {t("panel.days.showing")} {total} {t("panel.days.unit")}
        </Text>
      </Stack>

      <div
        style={{
          border: "1px solid #f6d4e0",
          borderRadius: "12px",
          background: "#fff",
          overflow: "hidden",
        }}
      >
        {/* 表头 */}
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "110px 52px 1fr 64px 96px",
            gap: "8px",
            alignItems: "center",
            padding: "0 10px",
            background: "rgba(255,158,184,0.10)",
            borderBottom: "1px solid #f6d4e0",
            fontWeight: 600,
            fontSize: "12px",
            color: "#a06a7c",
            lineHeight: ROW_LINE + "px",
          }}
        >
          <div>{t("panel.days.columns.date")}</div>
          <div>{t("panel.days.columns.turns")}</div>
          <div>{t("panel.days.columns.title")}</div>
          <div>{t("panel.days.columns.diary")}</div>
          <div>{t("panel.days.columns.view")}</div>
        </div>

        {/* 鼠标滚轮上下滚动区：显示区域放宽到可容纳 7 行 */}
        <div
          style={{
            overflowY: "auto",
            overflowX: "hidden",
            maxHeight: ROW_LINE * VISIBLE_ROWS + "px",
          }}
        >
          {days.map((row) => {
            const isSelected = selected === row.date
            const hasChapter = !!row.chapter
            const hasCatDiary = !!row.cat_diary
            return (
              <div
                key={row.date}
                style={{
                  display: "grid",
                  gridTemplateColumns: "110px 52px 1fr 64px 96px",
                  gap: "8px",
                  alignItems: "center",
                  padding: "0 10px",
                  borderBottom: "1px solid #fbe0e9",
                  background: isSelected ? "rgba(255,158,184,0.12)" : "#fff",
                  fontSize: ROW_FONT + "px",
                  lineHeight: ROW_LINE + "px",
                }}
              >
                <div
                  style={{
                    fontWeight: 600,
                    color: isSelected ? "#d7476f" : "#4a2a33",
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                  }}
                >
                  {row.date || "-"}
                </div>
                <div>{row.turns ?? 0}</div>
                <div
                  style={{
                    color: "#b08291",
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                  }}
                >
                  {hasChapter
                    ? truncate(row.chapter_title || t("panel.view.empty"))
                    : t("panel.no")}
                </div>
                {/* 日记列：自动日记 + 猫娘日记各一个小标记 */}
                <div style={{ whiteSpace: "nowrap", fontSize: "11px" }}>
                  {row.diary ? t("panel.yes") : t("panel.no")}
                  {hasCatDiary ? (
                    <span
                      style={{
                        marginLeft: "4px",
                        color: "#e89ab5",
                        fontWeight: 600,
                      }}
                      title={t("panel.view.catDiary")}
                    >
                      喵
                    </span>
                  ) : null}
                </div>
                <div style={{ whiteSpace: "nowrap" }}>
                  {selectDay ? (
                    <ActionButton action={selectDay} values={{ date: row.date || "" }} />
                  ) : null}
                </div>
              </div>
            )
          })}
          {days.length === 0 ? (
            <div
              style={{
                padding: "0 10px",
                fontSize: ROW_FONT + "px",
                lineHeight: ROW_LINE + "px",
                color: "#9aa3b5",
              }}
            >
              {t("panel.days.empty")}
            </div>
          ) : null}
        </div>
      </div>
    </Stack>
  )
}

export default function NekoDailyChroniclePanel(props: PluginSurfaceProps<ChronicleState>) {
  const { t, state, actions } = props

  const days = state?.days || []
  const llm = state?.llm || {}
  const paths = state?.paths || {}
  const selected = state?.selected || ""

  const findAction = (id: string) => actions.find((item) => item.id === id) as HostedAction | undefined
  const selectDay = findAction("select_day")
  const checkLlm = findAction("check_llm")
  const saveMemo = findAction("save_memo")
  const saveTitle = findAction("save_title")
  const saveDiary = findAction("save_diary")
  const catWriteDiary = findAction("cat_write_diary")
  const fetchMemoNow = findAction("fetch_memo_now")
  const capsuleWrite = findAction("capsule_write")

  // 备忘录 / 标题 / 日记的可编辑本地状态（按所选日期同步）
  const [dayKey, setDayKey] = useLocalState("memoDay", () => state?.selected || "")
  const [memoText, setMemoText] = useLocalState("memoText", () => state?.memo_text || "")
  const [memoTitle, setMemoTitle] = useLocalState("memoTitle", () => state?.memo_title || "")
  const [diaryText, setDiaryText] = useLocalState("diaryText", () => state?.diary_text || "")
  if (state?.selected && state.selected !== dayKey) {
    setDayKey(state.selected)
    setMemoText(state.memo_text || "")
    setMemoTitle(state.memo_title || "")
    setDiaryText(state.diary_text || "")
  }

  // 日记切换：auto = 自动生成的日记，cat = 猫娘自己写的日记（任务2）
  const [diaryMode, setDiaryMode] = useLocalState<"auto" | "cat">("dm", () => "auto")
  // 追加到猫娘日记的输入（猫娘日记只追加不覆盖）
  const [catAppend, setCatAppend] = useLocalState("ca", () => "")
  // 时间胶囊：正文 + 解锁日期
  const [capsuleText, setCapsuleText] = useLocalState("capText", () => "")
  const [capsuleDate, setCapsuleDate] = useLocalState("capDate", () => "")

  const endpoint = llm.endpoint || {}
  // 时间胶囊按作者分组：只有猫娘（catgirl）写的才代表「她写好了」。
  const capsuleItems = state?.capsules || []
  // 模型状态：可用性以"真实探测结果"为准，而不是只看是否配置了端点。
  //   probed === true  → 真能用（绿"可用"）
  //   probed === false → 已配置但调用失败（红"不可用"，带原因）
  //   probed 为空      → 还没探测过（橙"待检查"）
  const llmProbed = llm.probed === true || llm.probed === false ? llm.probed : null
  const llmUsable = llmProbed === true
  const llmUnusable = llmProbed === false
  const llmModelText = `${endpoint.model || ""}${endpoint.base_url ? " @ " + endpoint.base_url : ""}`
  const llmText = llmUsable
    ? `${t("panel.llm.ready")} ${llmModelText}`
    : llmUnusable
      ? `${t("panel.llm.configuredButUnavailable")} ${llm.probe_detail || llm.detail || ""}`
      : llm.available
        ? `${t("panel.llm.ready")} ${llmModelText}（${t("panel.llm.notChecked")}）`
        : `${t("panel.llm.local")} ${llm.detail || ""}`

  const catDiaryDays = state?.cat_diary_days || []
  const selectedHasCatDiary = !!state?.cat_diary_exists

  return (
    <Page title={props.plugin?.name || t("panel.title")} subtitle={t("panel.subtitle")}>
      <Grid cols={4}>
        <StatCard label={t("panel.stat.days")} value={state?.days_count || 0} />
        <StatCard label={t("panel.stat.chapters")} value={state?.chapters_count || 0} />
        <StatCard label={t("panel.stat.diaries")} value={state?.diaries_count || 0} />
        <StatCard label={t("panel.stat.todayTurns")} value={state?.today_turns || 0} />
      </Grid>

      <Card>
        {days.length > 0 ? (
          <DaysPager
            days={days}
            selected={selected}
            t={t}
            selectDay={selectDay}
          />
        ) : (
          <Stack>
            <Text>{t("panel.days.empty")}</Text>
            <Tip>{t("panel.days.emptyTip")}</Tip>
          </Stack>
        )}
      </Card>

      <Card title={`${t("panel.view.title")} ${selected}`}>
        {state?.selected_exists ? (
          <Stack gap={8}>
            <Text>{t("panel.view.chapter")}</Text>
            <pre
              style={{
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
                maxHeight: "30rem",
                height: "30rem",
                minHeight: "8rem",
                overflow: "auto",
                overflowY: "auto",
                background: "rgba(0,0,0,0.03)",
                padding: "10px",
                borderRadius: "6px",
                border: "1px solid #f6d4e0",
                fontFamily: "monospace",
                fontSize: "13px",
                lineHeight: "1.6",
                margin: 0,
              }}
            >
              {state?.chapter_text || t("panel.view.empty")}
            </pre>
            {state?.chapter_truncated ? <Warning>{t("panel.view.truncated")}</Warning> : null}

            <Divider />

            {/* 日记切换按钮（任务2）：借鉴 neko_diary 的胶囊分段控件 */}
            <Stack direction="row" gap={10} style={{ alignItems: "center", flexWrap: "wrap" }}>
              <span style={segWrap}>
                <button style={segBtn(diaryMode === "auto")} onClick={() => setDiaryMode("auto")}>
                  {t("panel.view.diaryAuto")}
                </button>
                <button style={segBtn(diaryMode === "cat")} onClick={() => setDiaryMode("cat")}>
                  {t("panel.view.catDiary")}
                </button>
              </span>
              {diaryMode === "cat" ? (
                <Text color="muted" style={{ fontSize: "12px" }}>
                  {selectedHasCatDiary
                    ? `${t("panel.view.catHint")}（${catDiaryDays.length} ${t("panel.days.unit")}）`
                    : t("panel.view.catEmpty")}
                </Text>
              ) : null}
            </Stack>

            {diaryMode === "cat" ? (
              /* 猫娘自己的日记：只读展示 + 追加输入（只追加、不覆盖） */
              <Stack gap={8}>
                <pre
                  style={{
                    whiteSpace: "pre-wrap",
                    wordBreak: "break-word",
                    maxHeight: "30rem",
                    minHeight: "6rem",
                    overflow: "auto",
                    background: "rgba(244,166,192,0.06)",
                    padding: "10px",
                    borderRadius: "6px",
                    border: "1px solid #f4d5e2",
                    fontFamily: "monospace",
                    fontSize: "13px",
                    lineHeight: "1.6",
                    margin: 0,
                  }}
                >
                  {state?.cat_diary_hidden
                    ? t("panel.view.catHidden")
                    : state?.cat_diary_text || t("panel.view.catEmpty")}
                </pre>
                {state?.cat_diary_truncated ? <Warning>{t("panel.view.truncated")}</Warning> : null}
                <Text>{t("panel.view.catAppend")}</Text>
                <textarea
                  value={catAppend}
                  placeholder={t("panel.view.catAppendPlaceholder")}
                  rows={6}
                  onChange={(event: any) => setCatAppend(event.target.value)}
                  style={{ width: "100%", padding: "8px", borderRadius: "8px", border: "1px solid #f6d4e0", fontSize: "13px", lineHeight: "1.6", fontFamily: "monospace", maxHeight: "16rem", overflowY: "auto" }}
                />
                {catWriteDiary ? (
                  <ActionButton action={catWriteDiary} values={{ date: selected, text: catAppend }} />
                ) : null}
              </Stack>
            ) : (
              /* 自动生成的日记：可编辑 + 保存 */
              <Stack gap={8}>
                <Text>{t("panel.view.diary")}</Text>
                <textarea
                  value={diaryText}
                  placeholder={t("panel.view.empty")}
                  rows={12}
                  onChange={(event: any) => setDiaryText(event.target.value)}
                  style={{ width: "100%", padding: "8px", borderRadius: "8px", border: "1px solid #f6d4e0", fontSize: "13px", lineHeight: "1.6", fontFamily: "monospace", maxHeight: "30rem", overflowY: "auto" }}
                />
                {state?.diary_truncated ? <Warning>{t("panel.view.truncated")}</Warning> : null}
                {saveDiary ? (
                  <ActionButton action={saveDiary} values={{ date: selected, text: diaryText }} />
                ) : null}
              </Stack>
            )}
          </Stack>
        ) : (
          <Stack>
            <Text>{t("panel.view.empty")}</Text>
            <Tip>{t("panel.view.emptyTip")}</Tip>
          </Stack>
        )}
      </Card>

      <Card title={t("panel.memo.title")}>
        <Stack gap={8}>
          <Text>{t("panel.memo.hint")}</Text>
          <Text>{t("panel.memo.titleField")}</Text>
          <input
            value={memoTitle}
            placeholder={t("panel.memo.titlePlaceholder")}
            onChange={(event: any) => setMemoTitle(event.target.value)}
            style={{ width: "100%", padding: "6px 10px", borderRadius: "8px", border: "1px solid #f6d4e0", fontSize: "13px" }}
          />
          <Text>{t("panel.memo.text")}</Text>
          <textarea
            value={memoText}
            placeholder={t("panel.memo.textPlaceholder")}
            rows={12}
            onChange={(event: any) => setMemoText(event.target.value)}
            style={{ width: "100%", padding: "8px", borderRadius: "8px", border: "1px solid #f6d4e0", fontSize: "13px", lineHeight: "1.6", fontFamily: "monospace", maxHeight: "30rem", overflowY: "auto" }}
          />
          <Stack direction="row" gap={8} style={{ flexWrap: "wrap", alignItems: "center" }}>
            {saveTitle ? (
              <ActionButton action={saveTitle} values={{ date: selected, text: memoTitle }} />
            ) : null}
            {saveMemo ? (
              <ActionButton action={saveMemo} values={{ date: selected, text: memoText }} />
            ) : null}
            {fetchMemoNow ? <ActionButton action={fetchMemoNow} /> : null}
          </Stack>
        </Stack>
      </Card>

      <Card
        title={`${t("panel.capsule.title")}${
          state?.capsules_due ? ` · ${state.capsules_due} ${t("panel.capsule.due")}` : ""
        }`}
      >
        <Stack gap={8}>
          <Text>{t("panel.capsule.hint")}</Text>
          <textarea
            value={capsuleText}
            placeholder={t("panel.capsule.placeholder")}
            rows={5}
            onChange={(event: any) => setCapsuleText(event.target.value)}
            style={{ width: "100%", padding: "8px", borderRadius: "8px", border: "1px solid #f6d4e0", fontSize: "13px", lineHeight: "1.6", fontFamily: "monospace", maxHeight: "14rem", overflowY: "auto" }}
          />
          <Stack direction="row" gap={8} style={{ flexWrap: "wrap", alignItems: "center" }}>
            <Text color="muted">{t("panel.capsule.unlock")}</Text>
            <input
              type="date"
              value={capsuleDate}
              onChange={(event: any) => setCapsuleDate(event.target.value)}
              style={{ padding: "6px 10px", borderRadius: "8px", border: "1px solid #f6d4e0", fontSize: "13px" }}
            />
            {capsuleWrite ? (
              <ActionButton
                action={capsuleWrite}
                values={{ text: capsuleText, unlock_date: capsuleDate }}
              />
            ) : null}
          </Stack>
          <Divider />
          {capsuleItems.length === 0 ? (
            <Text color="muted">{t("panel.capsule.empty")}</Text>
          ) : (
            [
              { key: "catgirl", items: capsuleItems.filter((item) => item.author === "catgirl") },
              { key: "master", items: capsuleItems.filter((item) => item.author !== "catgirl") },
            ]
              .filter((group) => group.items.length > 0)
              .map((group) => (
                <Stack key={group.key} gap={8}>
                  <Text color="muted" style={{ fontSize: "12px" }}>
                    {group.key === "catgirl" ? "🐾" : "💌"}{" "}
                    <strong>{group.items[0].author_name || "-"}</strong>
                  </Text>
                  {group.items.map((item) => (
                    <div
                      key={item.id}
                      style={{
                        border: "1px solid #f6d4e0",
                        borderRadius: "10px",
                        padding: "8px 10px",
                        background: "rgba(244,166,192,0.05)",
                      }}
                    >
                      <Stack direction="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
                        <Text color="primary">
                          <strong>{item.author_name || "-"}</strong>
                        </Text>
                        <Text color="muted" style={{ fontSize: "12px" }}>
                          {item.locked
                            ? `🔒 ${item.unlock_day} · ${t("panel.capsule.locked")}${
                                item.days_left ? `（${item.days_left} ${t("panel.capsule.daysLeft")}）` : ""
                              }`
                            : `✅ ${item.unlock_day} · ${t("panel.capsule.unlocked")}`}
                        </Text>
                      </Stack>
                      {item.locked ? (
                        <Text color="muted" style={{ fontSize: "12px" }}>
                          {t("panel.capsule.lockedHint")}
                        </Text>
                      ) : (
                        <pre
                          style={{
                            whiteSpace: "pre-wrap",
                            wordBreak: "break-word",
                            margin: "6px 0 0",
                            fontSize: "13px",
                            lineHeight: "1.6",
                            fontFamily: "monospace",
                          }}
                        >
                          {item.text || ""}
                        </pre>
                      )}
                    </div>
                  ))}
                </Stack>
              ))
          )}
        </Stack>
      </Card>

      <Grid cols={2}>
        <Card title={t("panel.paths.title")}>
          <KeyValue
            items={[
              { key: "root", label: t("panel.paths.root"), value: paths.root || "-" },
              { key: "chapters", label: t("panel.paths.chapters"), value: paths.chapters || "-" },
              { key: "diary", label: t("panel.paths.diary"), value: paths.diary || "-" },
              { key: "catDiary", label: t("panel.view.catDiary"), value: paths.cat_diary || "-" },
              { key: "raw", label: t("panel.paths.raw"), value: paths.raw || "-" },
              { key: "browse", label: t("panel.browse"), value: state?.ui_path || "-" },
            ]}
          />
          <Divider />
          <Tip>{state?.numbering_hint || t("panel.numberingHint")}</Tip>
        </Card>

        <Card title={t("panel.llm.title")}>
          <KeyValue
            items={[
              { key: "status", label: t("panel.llm.status"), value: llm.available ? t("panel.llm.ready") : t("panel.llm.local") },
              { key: "model", label: t("panel.llm.model"), value: endpoint.model || "-" },
              { key: "source", label: t("panel.llm.source"), value: endpoint.source || "-" },
              { key: "key", label: t("panel.llm.key"), value: endpoint.has_key ? t("panel.yes") : t("panel.no") },
              { key: "config", label: t("panel.llm.configPath"), value: llm.config_path || "-" },
            ]}
          />
          <Divider />
          <Tip>{llm.detail || ""}</Tip>
        </Card>
      </Grid>
    </Page>
  )
}