# example

占位 plugin。**存在的意义是表明本仓库的初衷是一个多 plugin marketplace**——`devloop` 是第一个真实 plugin，`example` 用最小骨架演示"再加一个 plugin 长什么样"。

## 内容

- `.claude-plugin/plugin.json` — Claude manifest（最小）
- `.codex-plugin/plugin.json` — Codex 占位
- `commands/hello.md` — 一个 trivial `/hello` 命令

## 使用

装上后输入 `/hello` 或 `/hello <name>` 验证 plugin 安装成功。

## 定位

保留作为新 plugin 的模板：真要加第二个真实 plugin 时，可照本目录骨架起步，或直接删掉本占位。
