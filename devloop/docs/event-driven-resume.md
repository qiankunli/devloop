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

### 唤醒机制：一次性后台任务"退出"

- **不能**用长驻 monitor 的 stdout 通知来唤醒：Claude Code 会把**运行中**长驻任务的最新 stdout **每回合重投**（实测一个事件 69 分钟内被投 362 次，上游 issue `anthropics/claude-code#66219`）。所以长驻 monitor 一律 `--quiet`，**只 persist 不通知**。
- **改用一次性进程**：一个 `run_in_background` 的进程盯状态总线的 delta，**变了就 print + 退出**。它退出 = 一条**终态完成通知**，把会话**重唤恰好一次**；任务已终态，没有"运行中 stdout"可被重渲染，因此**不重投**。
  - 已用最小实验验证：一次性任务退出唤醒一次，后续回合未见重投——与长驻 monitor 的每回合重投形成对照。
- 角色落点：**Perceive** = 现有 PR-sweep monitor（`scripts/poll_pr_status.py`，persist `.devloop/pr.json`）；**Wake** = 一个一次性 waiter，由会话在留下 follow-up 时用 `run_in_background` arm，读 monitor 写的事实源、检测 delta 即退出。

### 如何获取"用户自动续跑标记"

- 复用 Claude 的 **permission mode** 作为"是否自动续跑"的信号——这是用户已有的、表达"放手让它干"的开关，语义天然吻合。
- mode 只在 **hook payload** 里拿得到；唤醒（后台任务退出）这条路进来时，主循环并不直接知道当前 mode。所以：用一个轻 hook 在每轮把 `permission_mode` 写进状态总线，Execute 唤醒后读它。
- **易变点**：若平台日后提供"当前会话 mode 可查面"或更直接的自动续跑标记，这里就该换。

### 如何执行

1. 会话收尾一轮、留下"待续"时：把 follow-up 意图写进状态总线（watch 哪些信号 + next 干啥），并 arm 一个一次性 waiter。
2. waiter 检测到变化 → 退出 → 会话被唤醒。
3. Execute：读 follow-up + 读持久化的 mode → 判相关性 → auto 续跑 / 否则摆建议等确认。

### 与现有机制的接口

- 长在已有的 event seam（`hooks/lib/events.py` 的 `Event` + `dispatch`，producer 把"变化"扇出给 handler）上——seam 注释已预留"wake / 匹配 pending intent 是 **handler 的事**，不进 dispatch"。
- 状态总线 = `.devloop/`：monitor 写 `pr.json`；follow-up 意图、持久化的 mode 各自一个 segment。

### 已知局限 / 待平台补

- 缺一个 **producer 主动发起、幂等、一次性的 `resume(session, reason)`** 原语：目前只能用"一次性任务退出"近似 push，或用 `/loop` + ScheduleWakeup **轮询**近似。`#66219` 是其症状面——它废掉的是**事件驱动续跑**的唯一干净 push 通道，而非单纯的通知刷屏。
- mode 无"当前会话可查面"，只能靠 hook 持久化（可能 stale）。
