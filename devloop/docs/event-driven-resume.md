# 事件驱动的会话续跑（event-driven resume）

让会话能对**自身动作之外**的外部状态变化做出反应：外部事件发生 → 会话被唤醒 → 按用户意愿决定"自动续跑"还是"摆出建议等确认"。

本文分三层：**需求**（要解决什么）、**设计**（实现无关、相对稳定）、**实现**（依托 Claude Code、易变）。读设计看"为什么这么分"，读实现看"在 Claude 上当前怎么落地"。

---

## 一、需求

devloop 的 North Star 是把 AI coding 从**步级纠错**推向**需求级纠错**：deploy → 跑 e2e/eval/perf → 读 verdict → 自动纠正。这条闭环里，关键事件（部署完成、验证出分、PR/MR 合入）往往**发生在 AI 的单次动作之外**，由人或外部系统触发。

典型场景：一组 MR 由人审核合入（合并动作本就在 AI 范围外），devloop 的 monitor 已经能**感知**其状态变化。痛点在最后一公里——合入后，**正在跑的会话并不知道这件事**：既给不出下一步建议，也无法续跑闭环。

要的能力：

1. **感知**外部状态变化（已有 monitor 在做）。
2. 变化后**唤醒**会话——让它"知道这事发生了"。
3. 唤醒后，按**用户是否允许自动续跑**来决定：自动接着干，还是把下一步建议摆出来等人确认。

边界：合并 / 发版等**对外、不可逆**操作必须受控。唤醒可以无条件，**动作必须 gate**——不做无脑自动续跑。

---

## 二、设计（实现无关）

核心：把"感知 → 唤醒 → 决策行动"拆成三个角色，**只靠状态总线交接**，且 **唤醒（机制）与执行（策略）彻底分开**。

### 三个角色

| 角色 | 职责 | 性质 |
|---|---|---|
| **Perceive 感知** | 轮询外部源（forge / deploy / verdict），把"当前状态"落到状态总线 | 纯机制；只管事实，不管唤醒、不管动作 |
| **Wake 唤醒** | 状态相对上次记录有 delta 就唤醒会话**一次** | 纯机制；零业务语义 |
| **Execute 执行** | 唤醒后：判相关性 + mode gate → 续跑 / 摆建议 | 纯策略 |

### 两条关键原则

1. **唤醒无条件，动作看 mode。**
   - 只要"有变化"就唤醒——**Wake 不判断这个变化是不是当前会话关心的**（那是 Execute 的活）。Wake 越哑越好。
   - 唤醒后才看用户的"自动续跑标记"：开 → 自动续跑；关 → 摆出下一步**建议**，等人确认。
   - 无论标记开关，**唤醒都已发生**；区别只在唤醒后"动手"还是"等确认"。
   - 这与状态总线的成本原则（"token 是第一约束"）不冲突：那条原则管**做事时不浪费**，不管**要不要做事**——唤醒一轮花的 token，替代的是一次人工步骤级干预，正是这套设计要买的东西。该省的在醒来之后：Execute 判不相关就轻量收尾；producer 侧按 repo / follow-up 收窄订阅是后续优化项，不是唤醒的前置。

2. **wake ⊥ execute。** 唤醒是 runtime 机制、执行是 agent 策略，受不同约束。相关性过滤、mode gate、下一步动作**全在 Execute**；Perceive / Wake 保持哑而通用，同一套也服务 deploy / verdict 源。

### "act 干点啥"（相对稳定）

被唤醒并决定行动时，做的是会话**先前留下的待续意图（follow-up）**的下一步——例如：一组 MR 合齐 → 跑验证 / 收尾 / 触发下一阶段。具体动作因任务而异，但"**读 follow-up 意图 → 执行其 next**"这个形状是稳定的。

### 生命周期

```
Execute(收尾一轮, 留下 follow-up)  ──arm──▶  Wake(等一个"有变化")  ──fire──▶  Execute(唤醒, 决策)
        写"待续意图"到状态总线              只认"变了没"                读意图 + mode → 续跑/摆建议
```

Execute 出现在**两端**（arm 在前、resume 在后），Wake 是中间那段哑的。三者只经状态总线耦合，**各自可独立测、可替换**。

---

## 三、实现（依托 Claude Code）

> 本节绑定 Claude Code 当前的能力与限制，平台一变就可能要改。设计层（第二节）不受其影响。

### 唤醒机制（首选）：Channels —— 把事件 push 进会话

Claude Code 的 **channels**（research preview，v2.1.80+；[channels-reference](https://code.claude.com/docs/en/channels-reference)）是这套设计的原生答案。一个 channel 是一个 **MCP server**：CC 起它当子进程，它用 `notifications/claude/channel` 把外部事件 **push 进会话**，以 `<channel source="..." ...>` 标签落进上下文；**idle 会话就此自己起一轮，且内容 inline 随到**（不必回读状态总线就知道"啥变了"）。

- 已用最小 forge channel 实验验证：PR 状态一变 → idle 会话**没人敲键就自己醒**，拿到 `<channel source="forge" kind="pr_change">…</channel>` 并直接动手。
- 它是**正经 push**，不走被 `#66219` 每回合重投的 stdout-notification 通道；也**省掉一次性 waiter 那套 workaround**。
- 唤醒轮是**受 permission 模型约束的完整回合**——能动手（受权限提示），不是被动只读。
- 角色：**Perceive + Wake + Inform 三合一**落在 channel；**Execute** 仍是醒来那轮的事。
- **不强制 TS**：channel = MCP server，MCP 也有 **Python SDK**，devloop 可用 Python 写 forge channel、复用现有 forge facade / `pr.json`，**不必换栈**（官方示例是 TS/Bun 只是生态熟路，非技术约束）。
- **会话关了即丢**：channel 是纯 push，事件**只在会话开着时**到达——关掉终端，停机期间发生的变化**不会补投、直接丢失**。这是保留下面 waiter fallback 的第一理由：waiter 盯的是状态总线 delta（pr.json / review.json），下次起会话仍能从落差里看出"错过了啥"。
- **meta 只收 identifier**：`Notification.meta` 的 key/value 会成 `<channel …>` 标签属性，**连字符会被丢弃**——kind / meta 一律用下划线或字母（我们的 `pr_change` / `merge_blocked` / `review_done` 是安全的，别写成 `review-done`）。
- **保留**：preview，自定义 channel 要 `--dangerously-load-development-channels`、不在 allowlist、Team/Enterprise 需管理员开 → 故保留下面的 fallback。

### 代码落点（devloop）

通知机制抽成一个**端口**，channel 只是第一种实现（deploy / verdict 以后复用同端口）：

- **`hooks/lib/notify/`** —— notify 端口。`base.py`：`Notification`（content/kind/meta，只说"surface 什么"）+ `Notifier` 协议（`deliver`）；`channel.py`：`ChannelNotifier`（push 成 `notifications/claude/channel`）+ `run_channel`（MCP server handshake + `claude/channel` 能力的复用壳）。`mcp` lazy import，无依赖也能导入 / 测。
- **`scripts/forge_channel.py`** —— forge **生产者**（薄）。复用 monitor 的 `repos_to_poll` + `.devloop/pr.json` 与同一 change-key，变化时 build `Notification` 交给 `Notifier` deliver——只加通知，不二次 poll forge。
- **`scripts/review_channel.py`** —— code-review **生产者**（薄）。复用 `repos_to_poll`，盯各 repo 的 `.devloop/review.json`：后台 `run_review.py` 写出终态、且有可动作内容（findings / 文件失败 / error）时，build 一条**带 findings 详情**的 `Notification` 交给 `Notifier`——idle 会话醒来即拿到 findings，不必回读。change-key（`wake_key`）含 `generated_at`，同 sha 重跑也唤醒一次；clean / skipped / running 不唤醒（无可动作信号，留给下一轮 prompt 的 pull——见 `context/repo.py`）。
- 加一个 deploy / verdict channel = 写个生产者 + `run_channel(...)`，不碰 `notify/channel`。

**实验启用**（channel 是 preview，**不自动挂载**——以免给非 channel 用户起 MCP server / 强加 `mcp` 依赖）：需 `mcp` 包，并以 dev flag 起会话（forge / review 可同挂、按需取舍）：

```json
{ "mcpServers": {
  "forge":  { "command": "python3", "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/forge_channel.py",  "${CLAUDE_PROJECT_DIR}"] },
  "review": { "command": "python3", "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/review_channel.py", "${CLAUDE_PROJECT_DIR}"] }
}}
```
```bash
claude --dangerously-load-development-channels server:forge server:review
```

### 唤醒机制（fallback）：一次性后台任务"退出"

无 channel（未开 preview / 纯 Python 环境）时退回这条：

- **不能**用长驻 monitor 的 stdout 唤醒：CC 把**运行中**长驻任务的最新 stdout **每回合重投**（实测一事件 69 分钟 362 次，`#66219`）→ 故 monitor 设计成 **persist-only、不发 stdout 通知**（唤醒交给 channel，或下面的 waiter）。
- **改用一次性进程** `scripts/wait_for_pr_change.py`：盯 `.devloop/pr.json` 的 delta，**变了就 print + 退出** = 一条终态通知，**重唤恰好一次**（已验证不重投）。
- 与 channel 的差别：waiter 只 signal"变了"，Execute 得**自己回读 `pr.json`**；channel 则 inform 即带内容。
- 角色：**Perceive** = `scripts/poll_pr_status.py`；**Wake** = waiter，会话留 follow-up 时 `run_in_background` arm。

### mode gate：自动续跑 vs 等确认

- 唤醒轮跑在 **permission 模型**下，"auto 续跑 vs 等确认"天然落在它身上：auto-accept / bypass 类 mode → 醒来直接干；否则工具调用弹权限提示 = "等确认"。
- channel 的 **permission relay** 把审批 prompt 转到远端（手机 `yes <id>`/`no <id>`），补齐"等确认"的**异地**路径，不用自造确认 UI。
- fallback 无 relay：可用一个轻 hook 把 `permission_mode` 写进状态总线，Execute 醒来读它决策。

### 如何执行

1. 会话收尾、留"待续"时：follow-up 意图写进状态总线（watch 啥 + next 干啥）。channel 路径常驻 push；waiter 路径额外 arm 一个 waiter。
2. 事件到来 → 唤醒（channel 带内容；waiter 裸 wake + Execute 回读）。
3. Execute：读 follow-up（+ 必要时回读）→ 判相关性 → 按 permission mode 决定 auto 续跑 / 等确认。

### 与现有机制的接口

- 推给 agent 走 **notify 端口**（`hooks/lib/notify`：`Notification` + `Notifier`）——channel 是第一种投递（`ChannelNotifier` + `run_channel`）；producer（`scripts/forge_channel.py`）盯状态总线的变化、build `Notification` 交给 `Notifier`。channel 与 waiter 是这条推路的不同**出口**（前者带内容、后者裸 wake）。
- 状态总线 = `.devloop/`：monitor 写 `pr.json`；follow-up 意图、（fallback 下）持久化 mode 各一个 segment。

### 已知局限 / 仍待平台

- **producer 主动唤醒：channels 已基本补上**（preview）。`#60943`（payload-less `claude notify --type wake`）跟踪更轻的纯信号变体；`#66219` 是旧 stdout 路径的症状面。
- channels 仍 preview / allowlist 受限 → GA 前 fallback waiter 不能撤。
