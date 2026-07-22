# Board：上下文组织与投递

## 理念 / 概念

Board 是状态源之上的协作上下文读模型，prompt 与 UI 都只是消费者。它的边界是：
**状态源提供事实，Board 决定如何组织和投递**。

Git、forge、验证命令、review 与 workspace 解析器仍各自拥有事实；Board
不复制这些事实，也不参与硬门禁判定。它把相关事实投影成 payload-first 的
`BoardItem`：payload 是供 prompt、UI 等消费者共享的稳定读模型，prompt 文本只是
其中一个 renderer，不再是 Board 的事实形态。

原来的 `session / turn / event / ui_only` 不是同一维度，Board 将它们拆成正交策略：

| 维度 | 当前取值 | 回答的问题 |
|------|----------|------------|
| item kind | `state` / `event` / `detail` | 条目表达现在、一次性信号，还是展开细节 |
| delivery channel | `prompt` / `ui` | 条目可送到哪里 |
| prompt scope | `session` / `turn` | 进入 prompt 时按哪个节奏投递 |
| replay policy | compaction 后是否重放、最多投递次数 | 同一 revision 何时再次出现 |

例如 branch 是 `state + prompt/ui + turn`，review 完成是
`event + prompt/ui + turn + bounded delivery`，完整 PR 历史则只有 `ui` channel。

## 流程

`SessionStart` 预热 workspace/repo 事实。`BoardRuntime` 统一解析当前 cwd 与 session
focus，把事实投影为共享 `Board`，再得到相关 `BoardView`。每次
`UserPromptSubmit`，同一入口组合 branch、dirty、validation、当前 PR、review 等条目，
`DeliveryPolicy` 选择 prompt channel，`PromptDelivery` 只投递本 session 尚未收到或
已经变化的部分。

UI 读取 `BoardRuntime.snapshot()` 得到 JSON-ready 的结构化 view；读取不经过 prompt
renderer，也不改变 delivery receipt。Board HUD 的两个 adapter 消费同一个 snapshot：
Claude 原生 `statusLine.command` 每次从官方 stdin 取得当前 `cwd + session_id`，展示
focus 与健康状态两行；Codex 在 tmux 中启动时，`SessionStart` 创建固定三行只读
sidecar，前两行相同，第三行展示 watcher 观察到的最近一次 snapshot 变化。HUD 不直接
拼接各状态源，也不参与 prompt receipt。

投递游标按 session 存在 `.devloop/board/sessions/`。`PostCompact` 会让状态条目
在下一轮重放；已经消费的 event 不会因压缩再次触发。只有 UI channel 的条目不经过
prompt 投递，也不产生 receipt。

## 关键设计

### 相关优先，内容从小

Board 只投递当前工作所需信息。当前分支的 PR 会进入 branch 条目；与当前任务
无关的近期 PR 窗口仍保留在状态源中，供查询或后续 UI 展示，但不占 prompt
token。每个条目独立去重，某个事实变化不会捎带未变化的整块上下文。

相关性属于 `BoardView`，投递节奏属于 `DeliveryPolicy`；事实生产者不选择 channel、
prompt scope 或重放行为。Requirement 当前仅以兼容卡片加入 Board；其独立 provider
与领域抽象留到 requirement 域推进时处理，不以某个 UI adapter 是否完成为前置。

### 投递状态不是业务事实

Board 本体和 view 都不持久化事实副本。`DeliveryReceiptStore` 只保存每个 session 的
条目 revision、次数和投递时间；这些数据可以安全删除或过期。repo、branch、PR、
validation 等事实仍由原 owner 的 segment 提供。
硬门禁继续读取 live truth，不依赖 Board 的展示视图或投递游标。

### 状态与事件分开重放

branch、dirty、validation 等描述“现在在哪里”，compaction 后必须重放；review
结果和待办提醒描述“发生过什么 / 请做什么”，按身份限次投递，避免 agent
重复处理同一件事。

### HUD 是展示面，不是第二条状态总线

HUD 固定保留工作上下文与当前健康状态；失败或阻塞事实始终留在健康状态行，不能只作为
会被覆盖的实时消息出现。Codex sidecar 的第三行由常驻 watcher 比较前后两帧 Board item
revision 得出，仅在进程内保留最新一条，不另建事件 ledger；HUD 重启后从 “watching
Board”重新开始。Claude 每次 status line 刷新都会启动新进程，因此只渲染两条稳定状态，
不为凑齐第三行额外持久化一套 UI 事件游标。

Claude 原生 status line 通过一次性 `/devloop:setup-board` 配置；安装器备份 settings，
发现其它 status line 时必须经用户确认才替换。tmux 只补 Codex 尚未开放给 plugin 的
自定义展示位置：HUD pane 以 CLI session 与 leader pane 标识，重复 SessionStart 复用同一
pane，leader 回到 shell 后自动退出。非 tmux Codex 会话安静降级，不影响 Board 的 prompt
投递；两端均可通过 `board.hud.enabled=false` 显式关闭。
