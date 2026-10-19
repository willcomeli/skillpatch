# Stage 1 初版实现与验收

> 本文保留初版存档文本诊断的历史结果。2026-10-12 的验证后、清理前环境诊断实现及新运行见 [Stage1实时诊断验收.md](Stage1实时诊断验收.md)。

日期：2026-10-11。

## 实现范围

已打通：本次真实运行记录 → 核对并组织证据 → 单个诊断 Agent → 结构及引用校验 → 候选报告。覆盖整项 Skill／子步骤与 Under-use／Misuse，允许无支持冲突或证据不足。尚未生成、应用或验证补丁。

实现入口是 `skillpatch diagnose`。模块和命令见 [README.md](README.md#stage-1-诊断)，语义规则见 [prompts/discovery.md](prompts/discovery.md)。诊断核心使用统一 `DiscoveryInput`，当前真实接入的是 SkillsBench／BenchFlow，其他产物读取器可通过 `input_builder` 接入。

## 本次证据

使用本轮已重新执行的 [paper-anonymizer 运行](runs/20261011T062354Z-paper-anonymizer-4d0aa0bf/result.json)，不使用前期历史失败轨迹，也没有重新执行 benchmark。

- 执行记录：110 个 ACP 事件、64 次工具调用，两个预期 Skill 已实际读取。
- benchmark 反馈：6 项测试，5 通过、1 失败；`test_identifiers_redacted` 指出 paper2 中残留两个姓名；reward 0。
- 诊断输入：2 条任务证据、5 份 Skill 文本、110 条轨迹证据、4 条反馈，共 121 条。
- 任务与 Skill 快照哈希一致；规范化事件与原始事件逐条一致；反馈属于同一 trial。
- 模型输入不包含 oracle／验证器源码正文，也不包含前期人工案例分析或预设冲突答案。
- 原始 PDF 和图片内容未直接提供给文本模型；输入保留可见文本及缺失说明。底层任务执行没有原始 HTTP／token 遥测，本次独立诊断调用有完整的请求、响应和用量记录。

## 实际模型调用

使用独立的 `deepseek-flash`，`thinking=disabled`，`temperature=0`，`max_tokens=8192`。两次均 HTTP 200、`finish_reason=stop`。每次命令仅调用模型一次，无自动重试；第二次是在检查首次失败后修订通用提示词并明确重新运行，未提供首次输出作为参考答案。

| 项目 | 首次调用 | 提示词修订后调用 |
| --- | --- | --- |
| 目录 | [20261011T115517Z-c66f7a9f](runs/20261011T062354Z-paper-anonymizer-4d0aa0bf/discovery/20261011T115517Z-c66f7a9f/result.json) | [20261011T115624Z-22c85195](runs/20261011T062354Z-paper-anonymizer-4d0aa0bf/discovery/20261011T115624Z-22c85195/result.json) |
| 输入字符（含系统提示） | 283,937 | 284,246 |
| API prompt tokens | 90,934 | 91,101 |
| API completion tokens | 1,310 | 1,605 |
| API total tokens | 92,244 | 92,706 |
| 模型调用耗时 | 7.166 秒 | 8.220 秒 |
| 本地检查 | 一处 `other_findings` 引文不匹配，拒绝产生正式报告 | 结构与原文引用通过 |
| 模型语义输出 | 原始输出保留，不修补引用 | 1 条 Under-use 候选，0 条 Misuse 候选 |

两次 API 报告的 total tokens 合计 184,950；不推算费用。首次失败记录完整保留。修订提示词仅加强了适用前提的证据要求及行号／diff 原文引用要求，没有加入具体论文、姓名或预期冲突。

第二次的产物：

- [候选报告 Markdown](runs/20261011T062354Z-paper-anonymizer-4d0aa0bf/discovery/20261011T115624Z-22c85195/report.md)
- [候选报告 JSON](runs/20261011T062354Z-paper-anonymizer-4d0aa0bf/discovery/20261011T115624Z-22c85195/report.json)
- [归档输入](runs/20261011T062354Z-paper-anonymizer-4d0aa0bf/discovery/20261011T115624Z-22c85195/input.json)
- [结构与引用校验](runs/20261011T062354Z-paper-anonymizer-4d0aa0bf/discovery/20261011T115624Z-22c85195/validation.json)
- [独立审阅记录](runs/20261011T062354Z-paper-anonymizer-4d0aa0bf/discovery/20261011T115624Z-22c85195/review.md)

## 语义复核结论

**工程流程跑通，但本例的一条 Under-use 候选尚不受充分证据支持，暂不进入 Stage 2。**

模型把两个残留姓名归入“致谢姓名遗漏”，但输入中姓名只出现在验证器反馈里，没有展示它们所在的章节及相对于 References 的位置。Skill 同时要求保留 References，因此位置是决定该条款是否适用的关键条件。报告一边将适用条件写成事实，一边在不确定项中承认无法确认，存在矛盾。

`T000022`、`T000028`、`T000039` 支持所列模式没有包含这两个姓名；`F0001`、`F0002`、`F0004` 支持验证器报告姓名残留，但它们是同一反馈的不同呈现，不能补足位置证据。此时既不能确认 Under-use，也不能反向认定验证器有错。

另一个 Agent 仅根据归档 `input.json` 和 `report.json` 做了独立审阅，结论一致。这是开发阶段的模型辅助复核，不是人工标注或实验验证。原模型报告保持原样，本地检查通过只代表结构／引用有效。

## 输入版本与可见性

两次真实调用的输入摘要均为：

`sha256:adabeb7ab7129b5e145982a68e42a484ee57db9c1ac68cc7f8e53eded6df4cd1`

调用后发现轨迹里有 8 次图片查看仅留下 `[Image: 1 URLs]` 占位符。初始输入已说明 PDF 未直接提供，但没有逐项标明这些图片。已补充占位符限制及“事件齐全不代表底层产物可完全观察”的说明；不修改历史归档，不为此再次调用模型。

最新代码完成了 [prepare-only 核验](runs/20261011T062354Z-paper-anonymizer-4d0aa0bf/discovery/20261011T115955Z-b3eeb049/result.json)：121 条证据、285,742 个输入字符，摘要为：

`sha256:3ed0c6a190b5a340ff56405ba45bfa51f96908f7885a8116478286fb46a31ef0`

## 工程检查及实证边界

最终执行 `PYTHONPATH=src python3 -m unittest discover -s tests -q`，143 项测试全部通过，耗时 4.216 秒。

工程检查使用 Python 标准库 `unittest`，覆盖来源／快照核验、部分证据、图像占位符、双类冲突合同、唯一条款和逐字引用、独立输入采集器、输入预算、模型异常／截断、凭据保护、失败留档及目录复用拒绝。测试中的模型调用均使用模拟响应，只有上表两次是实际 API 调用。

本次单个任务不能证明跨 benchmark 泛化，模拟 Misuse 合同不能视为真实 Misuse 案例。下一步需要补足本例的适用条件证据，并评估诊断提示词和模型是否能稳定地把缺少关键前提的情况保留为待证问题；补丁效果仍需后续实际执行验证。
