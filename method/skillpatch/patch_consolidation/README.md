# 补丁整合候选方案

本模块迁移旧版 Stage3 的来源读取、目标绑定和整合 Agent，使用公共 AgentClient。
当前输入是新版生成的候选补丁，不要求或声称它们已经通过执行验证。

```text
已有 generation/patch.json + 新 generation/patch.json
  → source.py：读取各自生成存档和 Stage1 context，重新核对来源
  → bind_target：明确 skill_key，同一主声明、目标文件、scope 和原文锚点
  → patch_consolidation.py：独立 harness 会话
  → keep / merge / defer 候选方案
```

`keep` 保留各条补丁；`merge` 提议用两段新文本替换这些补丁；`defer` 暂缓整合。
三者均不修改原补丁、原始 Skill 或任何生效版本。

## 调用

```bash
python -m set_up.cli stage3 \
  --existing /path/to/first-generation/generation/patch.json \
  --incoming /path/to/new-generation/generation/patch.json \
  --skill-key date-skill \
  --config configs/skillpatch-stage3.toml \
  --output /path/to/new-consolidation-output \
  --env-file /path/to/model.env
```

多个已有补丁可以重复传 `--existing`。每个补丁需要保留完整的生成存档及其中指向的
Stage1 存档；不需要重新访问原始 benchmark 结果。配置文件指向独立的整合 Agent 配置，
后者沿用公共 `[diagnosis]` 格式设置 harness、模型、提示词和会话时限。

每份 context 以 source_0、source_1 等单独交付，request 文件提供补丁和来源集合映射。
相同的 T/S 编号不会跨轨迹混用。各份材料完整保存，不做总字符数截断。

## 目标绑定的范围

需要调用者显式提供 Skill 身份 skill_key，并核对完整主 SKILL.md 文本、目标文件文本、
文件在 Skill 内的相对路径、scope 和精确引用锚点。不同锚点保守地留给后续处理。
这是基于已有 context 的文本绑定，不等同于旧版注册表中的完整 Skill 包版本或结构对象 ID；
不推断缺失的二进制资源、原始资源包，也不迁移历史 inline 补丁位置映射。

## 输出与后续

- sources.json：所有补丁与对应诊断来源，保留来源关联。
- target_binding.json：本次整合的共同目标及绑定范围。
- proposal/input/：问题、补丁及各份 context。
- proposal/agent/：harness 会话记录。
- proposal/proposal.json：候选方案、完整源补丁记录、检查范围。
- result.json：运行状态及候选方案路径。

旧版 keep/merge/defer 输出校验保留。这里只检查结构、来源及目标绑定，不证明语义正确，
也不声称程序精确跟踪了模型读过的字符范围。ready_for_activation 始终为 false。
执行评分、合并前后保留性验证、渲染补丁集合、注册表版本提交和生效均留待单独迁移。
