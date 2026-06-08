# devloop plugin — 设计与开发指南

面向二次开发者。使用向（装 / 配 / 跑）见 [`README.md`](./README.md)；共享术语见 [`CONCEPTS.md`](./CONCEPTS.md)。

---

## 项目定位与边界

**面向场景（本质）**：devloop 服务于**聚合工作区 + 多 subproject**、且**可能有多个 CLI / session（claude / codex / opencode …）同时在操作同一个工作区**的开发。一个 workspace 文件夹挂着某项目的全部代码仓（subproject）；workspace 与每个 subproject 各有自己的 AGENTS.md（项目边界 / 子模块 / 关键约定，跨文件细节下沉到 `docs/` 由 AGENTS.md refer）。AGENTS.md 是**文字知识源**，面向人和 AI 说明"这里是什么、边界在哪、该读什么"；`.devloop/*.json` 是**结构化运行态**，由 hooks / scripts / monitors 从 git、GitLab、验证命令和 AGENTS.md 解析结果派生，面向注入和 guard 做快速决策。

开发者在这个 workspace 里转一个循环：

```
enter 某子模块 → 提需求(可能跨多个 subproject) → 开发 → commit / 建 MR(含 lint/test) → 人工到 GitLab merge → 下一轮
```

排查问题也在同一 workspace：用 as-ops / as-web-ops / infra-ops 等发现问题后，带着上下文切到对应 subproject 开发。**workspace 是开发与排查的共同根，subproject 是动手的落点**——devloop 的全部能力，都是为提高这个循环里 AI 的首次成功率而生。一轮循环的端到端时序（每拍触发什么事件、哪些 hook/script 参与、三条贯穿流程线）见 [`docs/loop.md`](./docs/loop.md)。

**两个杠杆**（怎么提高首次成功率）：

1. **状态总线消除信息滞后**：`.devloop/` 状态总线把当前 subproject 的 branch / 工作区 / 近期 MR / validation，加上 workspace 级的子项目清单 + AGENTS.md References 的解析结果，实时注入每轮 prompt——AI 改第一行前就掌握现状。
2. **硬拦截把软约定变成执行级边界**：PreToolUse `deny`，AI 绕不过（保护分支、过期分支改文件、误带文件…）。

**实现取向**：native-first——承载这两个杠杆的机制全部坐到 Claude Code 原生事件（CwdChanged / PostCompact / FileChanged / monitors）+ 两个统一 facade（gitcmd / gitlab）上，代码自证、文档薄。独立 `.devloop/` 命名空间，状态与其它工具互不干扰。

**边界**：
- **两级模型**：聚合工作区（Mode A，多 subproject）与单仓库（Mode B）都支持，按用户级配置（`~/.config/devloop/config.json` 的 `workspaces`）自动判定，workspace 可选。配置**不放 plugin 目录**——那是版本化 cache，`/plugin update` 即清零；workspace 根（非 git 仓 + AGENTS.md 带子项目表）在 SessionStart / cd / resolve 时自动注册，手工 init 不是前置。
- **跨 subproject 需求是常态，但 0.1 的多 repo 协同（fanout / 发包依赖顺序）仍是占位**——当前以"逐个 enter subproject 开发"承载。
- 只管循环里"在 subproject 内动手开发"这一段的 git / 工作区 / 验证效率；**不做**问题发现与 trace（as-ops / infra-ops 等）、部署、通用 git 教学。
- 当前 **Claude-only**（CLI-agnostic by construction：hook 不读 plugin-root env、payload 只用公共子集、`${CLAUDE_PLUGIN_ROOT}` 占位；Codex 跟上加个 manifest 即可，0.1 不投入）。

---

## 代码地图与核心模块

```
devloop/
├── .claude-plugin/plugin.json     # Claude manifest（0.0.1；靠目录约定自动发现）
├── hooks/
│   ├── hooks.json                 # 事件注册
│   ├── lib/                        # CLI-agnostic 纯逻辑（无协议依赖，sys.path 自定位）
│   │   ├── hook_io.py             #   ★hook harness：guard / inject / observe / run 四个 runner
│   │   ├── gitcmd.py              #   ★统一 git runner（超时 + failure-safe，唯一 git 入口）
│   │   ├── gitlab/                #   ★统一 GitLab facade（唯一 GitLab 入口）
│   │   │   ├── client.py          #     传输缝：auth + origin→project 解析 + request + 错误
│   │   │   └── mr.py              #     MR 操作（python-gitlab / GitLab-MCP 命名，含 window）
│   │   ├── cmdparse.py            #   ★guard 用：shlex 分词 + git 全局选项识别（防引号误判 / 抓 git -C）
│   │   ├── repo_resolve.py        #   ★脚本的 cwd 无关 repo 解析（--repo 名/路径 → cwd 仓 → last-active）
│   │   ├── git_state.py  parsers.py  repo_layout.py  workspace.py  session_lock.py
│   │   └── context/               #   .devloop/ 状态层：repo 级按 owner 分段，workspace 级单文件（base / repo / workspace）
│   ├── cwdchanged_enter.py        # CwdChanged：自动 enter
│   ├── sessionstart_init.py       # SessionStart：References(additionalContext) + watchPaths + 预热
│   ├── userprompt_inject.py       # UserPromptSubmit：turn + session 注入
│   ├── postcompact_reinject.py    # PostCompact：清注入 dedup
│   ├── filechanged_refs.py        # FileChanged：AGENTS.md 变更重注入
│   ├── posttool_git_refresh.py / posttool_track_edits.py   # PostToolUse 状态写入
│   └── pretool_*.py               # 10 个硬拦截（guard harness；含 checkout/edit owner 锁）
├── scripts/                        # smart_git_ops + smart_*.sh / read_mr / update_mr / run_fixlint / run_tests / poll_mr_status / init_*
├── monitors/monitors.json          # ★MR-sweep 后台轮询（替代 hook 心跳 scheduler）
├── commands/                       # slash：enter / gcam / gcamp / gcampr / lint / test
├── skills/                         # git-ops / gcam / gcamp / gcampr / fix-lint / run-test
└── config/                         # config.example.json 模板；运行时配置统一在用户级 ~/.config/devloop/config.json
```

---

## 关键约定

### 1. hook harness（消样板）
每个 hook = 一个函数 + 一个 runner（`hooks/lib/hook_io.py`）：`guard(decide)`（PreToolUse，返回 deny 理由或 None，异常→放行 fail-open）、`inject(produce, event)`（返回注入文本）、`observe(handle)`（副作用，恒输出 `{}`）、`run(build, event)`（富 payload，如 SessionStart 的 additionalContext+watchPaths）。runner 保证 hook 永不打断用户工具调用。

### 2. 三个统一 seam（集中、规范、可替换）
- **git → `gitcmd`**：所有 git 子进程的唯一入口，超时 + failure-safe（rc=-1 不抛）。
- **GitLab → `lib/gitlab`**：所有 GitLab 访问的唯一入口。`client.py` 是唯一传输缝（auth / project 解析 / HTTP / 错误）；`mr.py` 操作名对齐 **python-gitlab SDK 与 GitLab MCP**，将来换 SDK/MCP 只改 `client.py`，调用点不动。脚本一律是 facade 的薄包装，**不散写 urllib**。
- **用户配置 → `lib/config`**：所有对外部依赖的配置（GitLab token / host）+ workspace 注册表 + precommit 门禁的唯一入口，统一落 `~/.config/devloop/config.json`（用户级，`/plugin update` 不清；`DEVLOOP_CONFIG_DIR` 可覆写）。token 读取 env `GITLAB_TOKEN` 优先于 config；host 默认从 origin 推断、config `gitlab.host` 可覆写。`workspace` / `client` / precommit gate 都委托它，**不各自读文件**。

### 3. native-first 事件映射（本插件的设计本质）
每个能力坐在最原生的原语上；传统 hook 的"绕路"被原生事件取代：

| 能力 | 旧机制（hook 绕路） | devloop 原生 |
|------|--------------|-------------|
| 进项目感知 | 正则解析 `cd` | **`CwdChanged`** 自动 enter |
| 防 compaction 丢状态 | TTL 安全网（定时猜） | **`PostCompact`** 清 dedup → 重注入 |
| AGENTS.md 变更 | mtime 轮询 | **`FileChanged`** + SessionStart `watchPaths` |
| session 注入（References） | 每轮 UserPromptSubmit 捎带 | **SessionStart `additionalContext`**（落 first prompt 前，位置稳） |
| MR 感知 / 分支失活 | hook 心跳 scheduler + tasks | **`monitors`** 后台轮询写 context |

### 4. 状态总线 `.devloop/`（文字源 → 结构化态）
两级（workspace / repo）。写入者：PostToolUse / SessionStart / CwdChanged / FileChanged / monitor。读取者：UserPromptSubmit（软注入）+ PreToolUse guards（硬决策）。
- **文字源与结构化态分层**：AGENTS.md（workspace 级 / repo 级各自的边界、清单、References）是文字知识源；`.devloop/` 只保存解析后的结构化结果与运行态，不复刻正文。文件布局见 CONCEPTS.md〈状态文件〉。
- **分段状态中心（为什么 repo 级不是单文件）**：repo 级状态按 writer-owner 拆成段文件、`load` 合并成视图——写入角色分散在不同进程，单文件逼出"读-改-写"、并发丢更新；一段一 owner 让每次写只碰不相交的文件，**多写者丢更新在结构上不可能、无需锁**。原子写 / fail-open 读的原语在 `context/base.py`。
- **脚本与 cwd 解耦**：session cwd 在聚合工作区常驻 workspace 根，smart 脚本一律不依赖 cwd——repo 按"显式 `--repo` → cwd 仓库 → 最近活跃仓"解析并在 PLAN 里自述来源。解析链见 CONCEPTS.md〈脚本的 repo 解析〉。
- **MR 模型**：单 anchor（`branch.mr_iid`）+ 近期窗口（`mrs`）**派生**失活 / 在途，不存 bool；`mr.json` 由 monitor 独占且按 branch 归属——切分支即自动失效、无人去清（gcampr 建 MR 后也只触发 monitor poll，不自己写）。字段语义与窗口规则见 CONCEPTS.md〈MR 模型〉。
- **注入两 cadence**：turn（branch/dirty/validation/MR 摘要，每轮，内容哈希 dedup + TTL 兜底）、session（References，SessionStart 发一次、变更才重发）。`PostCompact` 清两者。
- 字段 schema / TTL / cap 数值、段读写原语（`load/save_segment` / `_write_atomic`）都在 `hooks/lib/context/base.py`，文档不复述。

**成本原则（token 是第一约束，"非必要不注入"）**——prompt 注入是跟 AI 沟通最直接也最贵的通道，加任何东西进 prompt 前先过这几条：
1. **只注入当前 subproject + workspace 级**的 References / 状态；**绝不**注入其它 subproject 的 AGENTS.md 正文（多 subproject 场景下这是最大的省 token 点）。
2. **`watchPaths` / `monitor` 输出是给 harness 的指令 / 通知，不进模型 prompt**——所以"注册全部 subproject 的 AGENTS.md 监听""轮询全部 subproject 的 MR"都是 token-free 的；真正进 prompt 的只有当前 repo 的那点状态。
3. **列表一律 cap**（子项目清单 12、MR 窗口 5），长描述压成一行。
4. **只在内容变化时才注入**（最大的省 token 手段之一）：每段注入文本按内容哈希比上次，**一样就不发**（`Cadence.should_emit`）——RepoContext 也是这样，branch/dirty/validation/MR 没变的轮次零增量；session 段一会话发一次。仅 TTL 到期或 `PostCompact` 才强制重发兜底。
5. 新增任何注入前自问：这真得**每轮**进 prompt 吗？hook 内部读 context 决策不行吗？（硬规则 deny 通常不需要进 prompt。）

### 5. 不走原生通道的硬规矩
- AI **绝不**直接 `git commit/push`（guard 会拦）或散调 GitLab——commit/push/MR 一律走 `scripts/smart_*.sh`（内部用 gitcmd + facade，自陈 `PLAN:` banner）。保护分支（main/master/release*）判定见 CONCEPTS.md。
- **新分支基点由意图定，不由 HEAD 当前态定**：`--branch`（开新工作）一律 cut 自 `origin/<target>`（`--base` 显式栈式），与当前停在哪条分支无关——否则上一轮留下的 in-flight 分支会被当成基底、新 MR 夹带其提交（夹带在 push/MR 前由 `smart_git_ops` 外来提交自检拦下）。当前分支在循环里的四态流转（protected / healthy / in-flight / inactive）见 CONCEPTS.md〈分支状态流转〉。
- **Owner 锁（owner / guest session）**：多 CLI / session 并发操作同一 checkout 时，第一笔**变更动作**的 session 占有它，guest 的切分支与编辑被硬拦、引导去 worktree——锁保护 checkout 的可变面，防止两个 session 的改动混进同一工作树；enter / 只读不占有，避免假冲突。占有点、豁免与逃逸口见 CONCEPTS.md〈Owner / guest session〉，实现见 `hooks/lib/session_lock.py`。

---

## References

- 一轮循环端到端流程（事件 → hook/script → 状态）：[`docs/loop.md`](./docs/loop.md)
- 使用 / 安装 / 配置：[`README.md`](./README.md)
- 共享术语（repo_dir / repo_code_dir / 保护分支 / MR 模型 / `<PLUGIN_ROOT>`）：[`CONCEPTS.md`](./CONCEPTS.md)
- 仓库级（marketplace / 多 CLI）：[`../AGENTS.md`](../AGENTS.md)
- 完整方案与设计决策：plan 文档（开发者本地 `~/.claude/plans/devloop-plugin-0.1.md`）
