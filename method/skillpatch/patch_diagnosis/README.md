# SkillPatch Stage1（离线 Agent 版）

```text
运行存档 → DiagnosisAdapter → input/context.json
                              ↓ 文件路径和诊断规则
                       harness.AgentClient
                              ↓ 本机 OpenHands + 模型
                       agent/workspace/report.json
                              ↓ 直接保存模型输出
                       report.json + report.md
```

使用方式（在整理版根目录执行）：

```bash
python -m set_up.cli stage1 --run-dir runs/某次SkillsBench运行 \
  --config configs/skillpatch-stage1.toml --output runs/某次诊断 --prepare-only

python -m set_up.cli diagnose --run-dir /path/to/native-task-result.json \
  --config configs/skillpatch-stage1.toml --output runs/某次正式诊断 \
  --env-file /path/to/deepseek.env
```

stage1 和 diagnose 是同一入口的两个名称。执行器 CLI 的 prepare/run 保持原样。

## 迁移内容与边界

- `discovery.py`：组织输入、调用 Agent、直接保存模型结果。
- `config.py`：诊断阶段配置，引用公共 AgentConfig；session_timeout_sec 控制整个会话。
- `discovery_validation.py`、`tool/evidence_references.py`、`models.py`、`tool/reporting.py`、`tool/output_language.py`：从旧版迁移报告结构、原文引用和呈现逻辑。
- 新模型输出沿用旧版 summary/improvements，引用字段仍叫 evidence_id；其值对应输入 items 中的 context_id，目标使用 skill_ref。无需模型自己生成绑定路径、版本或改进编号。
- harness/agent_client.py 是各阶段可共用的会话调用层，当前只接 OpenHands。Codex 仅下载，尚未接线。
- 不再一次性发送完整材料，也没有 max_input_chars 拒绝条件；保留完整 context 文件，由 Agent 按需读取，允许在工作目录记录进度。harness 仍会限制工具输出并压缩历史；不保证全部原文同时驻留模型窗口。
- 只分析本次提供的材料，不重新执行任务、访问运行容器或自动保留额外任务产物。提示要求不查看其他任务和历史报告；本机 harness 不是文件访问隔离边界。
- 原生 Runner 的 Skill 文本会生成明确标记的 delivered-memory 快照，供引用和目标绑定。它不证明恢复了原始 Skill 文件，后续补丁部署仍需匹配同一基底文本。
- Stage1 不调用报告校验器：report.json 原样保存模型 JSON 对象，report.md 只负责展示。不检查字段结构、目标唯一性、字符位置或引用是否匹配；不生成 validation.json。旧版校验工具保留供历史代码使用，不参与当前 Stage1。结果标记为 candidate_diagnosis。
- Stage2、Stage3 的业务编排和命令尚未迁移，本次只为其提供公共 AgentClient。

## 会话完成与失败

Agent 必须在指定工作目录写出 JSON 报告，并正常给出终结回答。AgentClient 同时检查退出码、ConversationErrorEvent、最终回答和报告文件；不能只依赖进程返回 0。超时或取消会停止进程组并保留会话记录。没有自动重试或自动修改模型报告。

Stage1 保存输入内容摘要，Agent 会话结束后核验材料未被修改，再直接保存模型报告；保留 JSON 解析、会话完成和凭据检查。准备模式不会调用模型或要求凭据。

## 诊断与改进资格

由诊断适配器提供 `DiagnosisContext.evolution_eligibility` 和 `eligibility_reason`：

- eligible：有效评分且未完全成功，进入 Stage1。
- succeeded：明确成功，跳过。
- invalid：评分缺失、异常或冲突，跳过并记录原因。

SkillsBench 沿用旧版 reward_eligibility：scored 且 reward 是有限数字时，1 为成功，
其余数字（包括部分得分）为 eligible。不从 passed 字段推测 reward。执行或验证器错误为 invalid。
三个原生数据集使用官方 success；有 score 时检查归一化范围和一致性。
ScienceWorld 额外检查最终评分可用性，满进度但 success=false 不自动视作成功。
AppWorld 存在测试统计时，要求有效非空测试集合及一致的通过/失败计数，避免零测试被视为可改进失败。

跳过时不启动 Agent、不生成诊断报告；result.json 保存 status=skipped、reason=succeeded/invalid，
以及具体 eligibility_reason。CLI 返回 0，prepare-only 也遵循同一规则。
旧 include_successful_runs 配置项及 CLI 覆盖开关已移除。
task_succeeded 仅作为兼容元数据保留，不再负责 Stage1 准入判断。
