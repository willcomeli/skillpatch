# Stage2：生成候选 patch

当前只迁移生成部分。四个数据集使用相同的输入和代码路径：

```text
Stage1 的 report.json + input/context.json
  → source.load_source：选择一条改进/冲突/其他发现，绑定目标原文
  → patch_generation.generate_patch
  → session.run_session → 公共 AgentClient → 本机 OpenHands + 模型
  → patches.validate_generation：检查输出结构，附加目标与来源信息
  → generation/patch.json
```

程序检查目标和引用能否对应 context 原文，不证明诊断正确、模型实际读过全部引用或补丁有效。
Stage1 原始模型报告保持不变；Stage2 为生成有明确目标的补丁，在读取时做结构和引用绑定。
不重新构造 context，也不要求访问原始任务、执行结果或 Skill 文件。

## 调用

在 skillpatch-rebuild 目录中：

```bash
python -m set_up.cli stage2 \
  --diagnosis-dir /path/to/stage1-output \
  --issue-id I000001 \
  --config configs/skillpatch-stage2.toml \
  --output /path/to/new-stage2-output \
  --env-file /path/to/model.env
```

`I000001` 表示报告中第一条 improvement；旧版 conflict 使用其报告 ID，
other_findings 使用 `O000001` 等顺序编号。每次调用生成一个问题对应的候选补丁。

`configs/skillpatch-stage2.toml` 只指定 generation_config。
`configs/skillpatch-generation.toml` 设置 harness、模型、服务地址、密钥环境变量名、
会话时限和生成提示词；当前复用公共配置读取器，因此表名仍是 `[diagnosis]`。

## 输出

- `source.json`：选中的 Stage1 问题和输入摘要。
- `generation/input/`：交给 agent 的问题和完整 context 文件。
- `generation/agent/`：独立 harness 会话、日志和模型报告。
- `generation/response.json`：模型原始 JSON 输出。
- `generation/generation.json`：生成状态、原因、来源和检查范围。
- `generation/patch.json`：候选补丁，包括原文绑定目标、when_to_use、how_to_interpret。
- `result.json`：本次调用结果。

模型也可以返回 insufficient_evidence 或 unpatchable，此时不生成 patch.json。
输入不按全文字符数截断，agent 通过路径自行读取；仍受会话时限约束。

## 后续独立迁移

将 patch 渲染为 Skill 补充文件、带 patch 执行任务、比较评分、模型效果评价及基于评价修订，
均留到后续单独实现。当前 generated 仅表示候选补丁已生成。
