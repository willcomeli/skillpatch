# 原始数据来源

当前可用输入在 `skillnet/` 中，保存公开的 `*_indices.json` 和辅助 JSON。
使用 `--source-format skillnet-indices --source-dir data/scienceworld/raw/skillnet`
即可读取，另外显式传入 simplification 和步数策略。任务对象清单已经由此生成。

## 可选：作者的原始包装清单

这里预留给分集脚本要求的原始 SkillNet 派生文件：

- `train.json`
- `dev.json`
- `test.json`

当前目录没有这三个 EviSkill 包装文件。它们与公开索引不是同一种格式，
不能直接改名使用；重建入口负责将公开索引转换为 Runner 能读取的对象。
来源和任务身份核对结果见 [SOURCE_AUDIT.md](../SOURCE_AUDIT.md)。

获得包装文件后请保留原始内容，避免重新格式化导致来源哈希变化。
也可以把原始文件保存在其他位置，通过 `--source-dir` 指定，无须复制到这里。

三个文件的顶层都应是 JSON 列表。脚本按 train/dev 中每条记录的 `skillnet_index`
选择任务，并按 `task_family` 检查覆盖情况；记录还需保留执行器使用的任务名称、
variation 和 simplification 等字段。仅有自然语言任务描述不足以代替原始清单。

预期 SHA-256 记录在上一级脚本的 `EXPECTED_SOURCE_SHA256` 中。
目前尚未找到 EviSkill 包装文件的直接下载地址；公开的 SkillNet 上游文件地址
记录在 `skillnet/provenance.json`。
