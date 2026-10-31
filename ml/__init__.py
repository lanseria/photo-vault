"""ML worker 包：RAM++ 离散打标。

架构约定（见 README）：
  - worker 跑在宿主机（或任意能连 DB 和 vault 的机器），不进 app 镜像
  - 从 jobs 表拉任务（FOR UPDATE SKIP LOCKED），标签写回 asset_tags(source='ai')
"""
