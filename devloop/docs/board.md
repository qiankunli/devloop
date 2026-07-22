# Board：上下文组织与投递

## 理念 / 概念

Board 是状态源与 prompt 之间的读模型，边界是：**状态源提供事实，Board
决定如何组织和投递**。

Git、forge、验证命令、review 与 workspace 解析器仍各自拥有事实；Board
不复制这些事实，也不参与硬门禁判定。它只从当前 session 相关的事实中生成
结构化 `BoardItem`，再由统一 policy 决定条目送往 `session`、`turn`、`event`
或 `ui_only` surface。因此后续 UI 可以读取同一个 Board 视图，而不会成为
新的事实源。

| Surface | 语义 |
|---------|------|
| `session` | requirement 摘要、References、repo 地图等稳定上下文 |
| `turn` | branch、dirty、validation、当前 PR/deploy 等当前状态 |
| `event` | review 完成、verdict 失败、PR blocked、协作消息等一次性信号 |
| `ui_only` | 完整历史、详细 artifact、长描述；保留在 Board，但不进入 prompt |

## 流程

`SessionStart` 预热 workspace/repo 事实，并由 Board 投递 `session` surface 的
References 与子项目视图。每次 `UserPromptSubmit`，Board 按当前 cwd 或 session
绑定解析相关 workspace/repo，组合 branch、dirty、validation、当前 PR、review
事件等条目，只投递本 session 尚未收到或已经变化的部分。

投递游标按 session 存在 `.devloop/board/sessions/`。`PostCompact` 会让状态条目
在下一轮重放；已经消费的 `event` 不会因压缩再次触发。`ui_only` 不经过 prompt
投递 seam，也不产生游标。

## 关键设计

### 相关优先，内容从小

Board 只投递当前工作所需信息。当前分支的 PR 会进入 branch 条目；与当前任务
无关的近期 PR 窗口仍保留在状态源中，供查询或后续 UI 展示，但不占 prompt
token。每个条目独立去重，某个事实变化不会捎带未变化的整块上下文。

### 投递状态不是业务事实

Board 只持久化每个 session 的条目签名、次数和投递时间。这些数据可以安全
删除或过期；repo、branch、PR、validation 等事实仍由原 owner 的 segment 提供。
硬门禁继续读取 live truth，不依赖 Board 的展示视图或投递游标。

### 状态与事件分开重放

branch、dirty、validation 等描述“现在在哪里”，compaction 后必须重放；review
结果和待办提醒描述“发生过什么 / 请做什么”，按身份限次投递，避免 agent
重复处理同一件事。
