# Worktree 依赖环境

devloop 把 checkout 隔离扩展到依赖环境：临时 worktree 不只要有独立源码，还必须让验证进程
解析到**这个 checkout 对应的依赖**。本文只记录稳定模型；具体识别文件、命令和指纹文件名以
`hooks/lib/ecosystem/` 为事实源。

## 理念 / 概念

依赖环境属于 **code unit**，不是 repo 的单值属性。一个仓库可以同时包含 Go、Python、Node
unit，每个 unit 有自己的环境判据和恢复方式。

环境要守住两个不变量：

1. **归属正确**：验证不能静默借用另一个 checkout 的依赖。仓内 worktree 缺 `node_modules`
   时，Node 会向父目录继续解析；残留的 Python 虚拟环境也可能把 editable install 指回主
   checkout。两者都可能让检查看似通过、实际检查了错误的树。
2. **内容一致**：devloop 恢复过的环境必须与当前 manifest + lockfile 对齐；恢复使用 frozen
   语义，只验证和物化依赖，不改项目声明。

共享的是包管理器的内容缓存，不是另一个 worktree 的整个环境目录。每个 worktree 保留自己的
`node_modules` / `.venv` 视图；pnpm 等工具可在这些视图背后复用全局内容寻址存储。这样既避免
重复下载，也不会把 workspace link、editable path 或分支依赖图串到另一棵源码树。

## 流程

`/enter --worktree` 创建或复用 worktree 后，按 code unit 枚举并调用生态注册表的
`ensure_ready`。这是预热，降低进入后的第一次验证延迟。

真正的 correctness 关口在 `lifecycle.checks`：lint/test 找到可执行命令后、启动命令前再次
调用同一个 `ensure_ready`。环境已就绪时是一次轻量检查；缺失或 devloop 指纹过期时执行一次
frozen 恢复；失败则返回明确的 environment setup failure，不伪装成 TypeScript、lint 或测试
代码错误。

同一 lifecycle 相位会并发跑 lint/test，因此 `ensure_ready` 对每个 unit 做 single-flight，
锁内重查环境，只允许一个线程写同一份依赖目录。

## 关键设计

### 生态是语言差异的唯一入口

项目 manifest、语言展示、环境就绪、恢复命令和无 Makefile 时的 canonical 回落命令都由
`hooks/lib/ecosystem/` 提供。`repo_layout`、命令 guard、worktree 创建和 lifecycle check 只消费
这个接口，不各自维护一份 `package.json` / `uv.lock` 判断。

### 自动恢复必须可重复

Node unit 没有支持的 lockfile 时，devloop 不猜裸 install 命令，因为那可能生成或改写项目状态；
它把缺失依赖报告为环境问题。Go 的 module cache 天然跨 checkout 安全，不需要显式 prepare。

对用户自己准备、没有 devloop 指纹的现有环境保持 fail-open，避免接管主 checkout 的日常依赖
管理；一旦由 devloop 恢复并盖过指纹，manifest 或 lockfile 变化就会触发重新恢复。

### 包管理器优化由仓库选择

devloop 尊重仓库已经选择的包管理器和配置，不全局开启实验性选项。频繁创建 Node worktree 的
仓库可以自行启用 pnpm 的 global virtual store；devloop 每个 worktree 仍执行一次 frozen install，
由 pnpm 将本地依赖视图链接到共享 store。
