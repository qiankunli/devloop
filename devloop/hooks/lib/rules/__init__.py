"""rules —— 具体 Rule 子类的集合（外置于 core，可积累的"判子"面）。

`REGISTRY` 是引擎消费的全部规则；hook 把它传给 `engine.evaluate`，引擎按 target 类型路由。
新增规则 = 写一个 `Rule` 子类、登记进 REGISTRY，不碰 core。
分组（未来）：command/（命令侧 guard 迁入）· edit/（编辑侧 guard）· code/（代码 lint）。
"""
from lib.rules.code.layer_deps import LayerDepsRule

REGISTRY = [
    LayerDepsRule(),
]
