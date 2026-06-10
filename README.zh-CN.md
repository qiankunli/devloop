[English](./README.md) | [简体中文](./README.zh-CN.md)

# devloop

**给 AI 编码搭一条带护栏的开发循环。** 一个跨 CLI 的 plugin marketplace：`devloop` 是第一个（也是主力）plugin——坐在 Claude Code 原生事件上的开发者工作流，**GitHub（PR）与 GitLab（MR）都支持**（按 repo 的 origin 自动识别）；`example` 是占位 plugin，表明本仓库从一开始就按**多 plugin marketplace** 设计。

> 当前仅支持 Claude Code。设计理念 / 架构见 [AGENTS.md](./AGENTS.md)，各 plugin 细节见各自目录的 README。

## 要解决什么

用 AI agent 写代码时，真正吃掉时间的往往不是「AI 写得对不对」，而是三类**结构性损耗**：

1. **信息滞后**——AI 不知道当前 git / 工作区的真实状态，只能凭对话历史猜。典型翻车：AI 基于某个 feature 分支吭哧吭哧改了一通，结果这条分支的 MR 早在服务端被 merge 了、源分支也删了；AI 全程不知道，一直改到 commit 那一刻才被 preflight 拦下，被迫重新切分支、搬代码、重建 MR——这套「晚发现」的固定开销每次都在白交。
2. **软约定拦不住**——「别在 master 上直接 commit」「别 `git add -A`」写在 prompt / skill 里只是软引导。AI 决定不遵守时，你**没有任何执行级的拦截能力**。保护分支直接 commit、误带敏感文件进暂存区、在过期分支上继续改——这些都真实发生过。
3. **多 session 互相踩**——在同一个 workspace 上并行开多个 CLI session（或多个 agent）很常见，但它们共享 checkout 和状态：第二个 session 把分支切走、搅乱第一个 session 未提交的工作；A session 的无参命令静默解析到 B 刚碰过的仓。原生没有任何「谁占有谁」的仲裁。

## devloop 做什么——两个杠杆

- **状态总线消除信息滞后**：把当前子项目的 branch / 工作区 / 近期 MR / 验证状态，实时注入**每一轮** prompt，AI 改第一行代码前就掌握现状。
- **硬拦截把软约定变成执行级边界**：`PreToolUse` hook 直接 `deny`，AI 绕不过去。

两个杠杆共享同一个枢纽：一个 `.devloop/` 结构化状态总线。`git commit` / `cd` / 后台轮询时刷新一次的状态，可被后续 N 轮 prompt 注入、M 次保护分支判定复用，零额外成本。

损耗 3 由骑在同两根杠杆上的 **session 级状态**回答：每个 checkout 一把 owner 锁（guest 的切分支 / 编辑被硬拦、引导去 worktree）+ 每 session 一份 repo 绑定（A 的兜底解析永远不会落到 B 的仓）——详见下文「聚合工作区 + 多 session 是一等公民」。

## 几个值得一看的设计

**什么该硬拦、什么只软提示**——准则：*没有任何合法编辑场景的，硬拦；有合法例外的，软提示。* 当前分支永远处于四态之一：

| 态 | 含义 | 处理 |
|----|------|------|
| protected | main / master / release* | **硬拦** commit/push |
| healthy | 普通 feature 分支，还在开发 | 正常放行 |
| in-flight | 已建 PR/MR、等人工 merge | **软提示**（注入一行 `IN-FLIGHT`） |
| inactive | PR/MR 已 merged / closed | **硬拦** Edit/Write |

protected 和 inactive 能干净地硬拦——在它们上面编辑没有任何合法理由。in-flight 只能软提示，因为它有一个机器无法可靠区分的合法例外（你可能就是想 amend 自己这条 PR/MR），于是把「这条分支在途」的事实喂给 AI，让它自己选。

**结构性保证，不只靠提示**——新分支的基点**由意图决定，不由 HEAD 当前停在哪决定**：只要是开新工作（`--branch`），就**永远**从 `origin/<target>` 切，且新分支在 push / 建 PR 前会被自检只带本轮提交。所以哪怕 AI 完全没看那行 `IN-FLIGHT` 提示，从在途分支 fork 也不会把它的提交夹带进新 PR。

**聚合工作区 + 多 session 是一等公民**——一个 workspace 根挂着多个独立 git 子项目（常以软链接聚合）。脚本一律**不信 shell 的 cwd**（按「显式 `--repo` → cwd 所在仓 → **本 session** 最近活跃仓」解析；本 session 没有绑定时宁可要求显式 `--repo`，也不猜别的 session 的活动），并用 *owner 锁* 防止两个并发 session 的改动混进同一个工作树。单仓库形态也完全支持——按用户级配置自动判定，无需手工切换。

**native-first**——每个能力都坐在最原生的事件原语上，而不是绕路：

| 能力 | 旧机制（绕路） | devloop 原生 |
|------|---------------|-------------|
| 进项目感知 | 正则解析 `cd` | **`CwdChanged`** 自动 enter |
| 防 compaction 丢状态 | TTL 安全网（定时猜） | **`PostCompact`** → 重注入 |
| `AGENTS.md` 变更 | mtime 轮询 | **`FileChanged`** + `watchPaths` |
| PR/MR 感知 / 分支失活 | hook 心跳 scheduler | **`monitors`** 后台轮询 |

所有 git 走唯一入口 `gitcmd`，所有代码评审平台走唯一 facade `lib/forge`（GitHub / GitLab 平级 adapter，按 repo 分发），所有用户配置走唯一入口 `lib/config`。每个 guard 一律 **fail-open**——护栏坏了最坏是没拦住，但绝不堵死你的路。

## 往哪走 —— 从步骤级到需求级

devloop 把循环跑稳了，但**粒度**还停在步骤级——人依然要在每一步盯着纠偏。北极星是把干预从**步骤级**抬到**需求级**：人提需求，AI 自己开发 → 校验 → 读结果 → 闭环里自纠偏，人只在最后**验收**。

关键是「校验」这一环从静态检查（lint / test）爬到一个闭环能收敛的真实 **verdict**——而值得收敛的 verdict 不是单一的过 / 不过。四个平级、**可积累**的判定维，各答一个问题：

| 维度 | 答什么 | 怎么判 |
|------|--------|--------|
| **对错** | 接口 / 行为对不对 | 黑盒，打真实运行的系统 |
| **效果** | agent 产出好不好 | 黑盒（含 LLM-as-judge） |
| **容量** | 压力下扛不扛得住 | 黑盒 |
| **口味** | 是否按你想要的方式建的——设计 / 边界 / 命名 | 白盒，读 diff，不依赖 deploy |

两条边界让它不跑偏（也对得上业界正在收敛的 *levels-of-autonomy* / *eval-driven* / *spec-driven* 几条线）：**merge 留给人**——发布权永不进闭环；**AI 只改 code 去达标，绝不挪门槛本身**——spec 和阈值是人治的，和 merge 同侧。对标 L4「human as approver」，不是 L5。

devloop 是**循环机器**——状态总线、硬拦、run / 校验 / deploy 这些拍；verdict 的生产者是另一件可插拔的事。所以开放面很宽：更多判定维与传感器、把 verdict 作为反馈接回闭环、让黑盒校验能打真实系统的 deploy 拍、白盒口味判子。**如果你对这条前沿感兴趣，开个 issue 或 discussion——这里的点子，正是我们最想要的贡献。**

*相关方向（本项目借鉴并身处其中）：**agentic coding** / **autonomous coding agents**、**self-correcting** & **verifier-driven** loop、**eval-driven development**（**LLM-as-judge**）、**spec-driven development**，以及 **human-in-the-loop** AI software engineering 的 **levels of autonomy** 框架。*

---

## 安装（Claude Code）

```
/plugin marketplace add https://github.com/qiankunli/devloop.git
/plugin install devloop@devloop
```

可选地跑一次 init（不跑也行，hook 首次 `cd` 进 repo 时会自动初始化）：

```
# 形态 A：聚合工作区（一个根目录下挂多个 git 子项目）
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/init_workspace.py <your-aggregate-workspace>

# 形态 B：单 git 仓库
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/init_repo.py
```

Forge 相关功能（PR/MR 创建 / 状态注入）需要对应平台的 token（环境变量 `GITHUB_TOKEN` / `GITLAB_TOKEN`，或 `forges` 配置块）——统一配置在 `~/.devloop/config.json`，见 [devloop/README.md](./devloop/README.md)。

### Codex / opencode

marketplace 结构是 CLI 无关的：新 CLI 接入只需在仓库根加 `.<cli>-plugin/marketplace.json`（已有 `.agents/plugins/marketplace.json` 给 Codex、`.opencode/marketplace.json` 占位）+ 每个 plugin 子目录的对应 manifest。`devloop` 本身当前 **Claude Code only**（硬拦截 / 状态注入坐在 Claude 原生事件上）；Codex 侧目前只有 `example` 占位。

## Plugin 列表

| Plugin | 简介 | README |
|--------|------|--------|
| `devloop` | 开发者工作流：git/PR（GitHub + GitLab）+ cwd-aware enter + lint/test gates + 实时状态注入 + 执行级硬拦截（Claude-only） | [devloop/README.md](./devloop/README.md) |
| `example` | 占位 plugin，演示多 plugin marketplace 结构 | [example/README.md](./example/README.md) |

## 新增 plugin

见 [CONTRIBUTING.md](./CONTRIBUTING.md)。
