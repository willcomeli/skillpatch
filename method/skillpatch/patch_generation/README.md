# Stage2：生成候选 patch

Stage 2 接收一个 Stage 1 问题，输出候选补丁：

```text
Stage1 的 report.json + input/context.json
  → source.load_source：选择一条改进/冲突/其他发现，绑定目标原文
  → pipeline.run_stage2
  → 公共 session.run_session → AgentClient → 配置的 harness + 模型
  → patches.validate_generation：检查输出结构，附加目标与来源信息
  → generation/patch.json
```

程序检查目标和引用能否对应 context 原文，不证明诊断正确、模型实际读过全部引用或补丁有效。
Stage1 原始模型报告保持不变；Stage2 为生成有明确目标的补丁，在读取时做结构和引用绑定。
不重新构造 context，也不要求访问原始任务、执行结果或 Skill 文件。

## 两种生成策略

- `--induction standard` 默认调用一次生成提示词，输出 `when_to_use / how_to_interpret`。
- `--induction boundary-aware` 要求 Stage 1 已绑定锚点。依次生成 p0、提出 INV/CON 用例，存在用例时生成 p1。候选使用 `note / example`。两种格式分别整合和交付，完整的两策略评测入口见 [ScienceWorld](../../../configs/scienceworld/README.md)。

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
会话时限和生成提示词 `prompts/skillpatch/patch_generation.md`。配置表名沿用 `[diagnosis]`。

## 输出

- `result.json` 记录生成状态、原因、Stage1 路径和来源摘要。
- `generation/input/request.json` 保存本次选择的问题。完整上下文直接引用 Stage1 的 `input/context.json`。
- `generation/agent/` 保存 harness 会话、日志和模型报告。
- `generation/response.json` 保存模型原始 JSON 输出。
- `generation/patch.json` 保存候选补丁及其 Skill 原文锚点。

验证和整合也可读取包含 `source.json` 与 `generation/generation.json` 的旧归档。

模型也可以返回 insufficient_evidence 或 unpatchable，此时不生成 patch.json。
输入不按全文字符数截断，agent 通过路径自行读取；仍受会话时限约束。

## 候选验证

generated 表示候选补丁通过结构和来源绑定检查。SkillsBench 的 validate-patch 和 AppWorld 的
validate-appworld-patch 会在独立目录中交付普通版候选补丁并重跑任务。
