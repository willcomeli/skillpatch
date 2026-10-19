# SkillPatch

当前已实现**真实任务运行器、Stage1 诊断、Stage2 补丁生成与验证，以及 Stage3 补丁集合整合与版本管理**。任务明确成功时跳过自动 Stage1；失败诊断中的冲突可进入 Stage2。Stage3 接收已验证补丁，定位原始对象，按需去重、合并和复验，再保存可回退的补丁集合版本。原始 Skill 和配套资源始终保持不变。

Excel 案例已有一次真实 Stage2 对照：基线通过 6/8 项测试，候选通过 8/8 项，独立评价及产物检查支持目标修复。Stage3 的工程检查与真实登记记录见 [Stage3验收.md](Stage3验收.md)；多补丁合并的真实效果仍需后续实验。

## 使用

运行器只依赖 Python 3.11+ 标准库；BenchFlow 在自己的环境中安装和运行。无需修改原实验环境。

在本项目目录执行：

```bash
PYTHONPATH=src python3 -m skillpatch prepare --config configs/paper-anonymizer.toml
```

`prepare` 校验并复制输入，启用显式 Skill 提示时还生成可检查的原始／新增／最终 prompt，不调用模型或执行任务，结果状态是 `prepared`。

真实运行一次：

```bash
PYTHONPATH=src python3 -m skillpatch run \
  --config configs/paper-anonymizer.toml \
  --env-file /home/lihanlun123/workspace/experiments/skillsbench-with-skill/.env.deepseek-v4-flash
```

本机示例使用现有 BenchFlow 0.6.3、OpenHands、`deepseek/deepseek-flash`、Docker 和 paper-anonymizer。环境文件名保留原名称；模型选择由 TOML 控制。原环境文件的 `DEEPSEEK_THINKING=disabled` 保持原实验的非思考模式。

模型 ID 已按 [DeepSeek 官方文档](https://api-docs.deepseek.com/) 更新为 `deepseek-flash`。官方说明旧 V4 Flash 名称已路由到 V4.1 Flash，因此不能声称这次运行复现了旧模型权重。

每次自动生成独立运行目录，也可用 `--output <新目录>` 指定。已有目录直接拒绝，不恢复历史任务、不读取旧轨迹。固定单任务、并发 1、重试 0、single-shot；正常运行由 BenchFlow 调用任务自带验证器。

## 配置

参见 [configs/paper-anonymizer.toml](configs/paper-anonymizer.toml)。路径相对于配置文件解析；`bench_command` 是参数数组，通过子进程直接调用，不拼接 shell。

| 配置 | 含义 |
| --- | --- |
| `task.benchmark` / `task.path` | benchmark 适配器名称、任务目录 |
| `task.skills_dir`（可选） | 替换任务快照中的完整 Skill 包，保留源文件不变 |
| `task.skill_mode` | `with-skill` 或 `no-skill` |
| `task.skill_condition` | 实验条件说明；提示行为由 `explicit_skill_read` 控制 |
| `task.explicit_skill_read` | 默认 `true`；with-skill 时通过对应 harness 的增加器添加显式 Skill 调用提示；关闭时不增加 |
| `agent.name` / `agent.model` | 执行 harness 与模型，必须明确指定；增加器当前支持 `openhands`（别名 `oh`） |
| `agent.reasoning_effort`（可选） | 仅用于后端及 Agent 支持的推理等级；当前 OpenHands 的 ACP 接口不支持此选项 |
| `runtime.bench_command` | 当前环境中的 BenchFlow 命令 |
| `runtime.bench_python` | 安装 BenchFlow SDK 的 Python；启用实时诊断时必须指定 |
| `runtime.diagnosis_config` | 独立诊断配置路径；有效评分且 reward != 1 时，在清理前调用 Stage1 |
| `runtime.diagnosis_timeout_sec` | 整个实时诊断的时间上限；当前任务配置为 1800 秒，省略时兼容旧默认 900 秒 |
| `runtime.expected_backend_version`（可选） | 运行前要求匹配的版本 |
| `runtime.timeout_sec` | 后端命令整体时间上限，包括环境准备、执行、验证和实时诊断 |
| `runtime.agent_idle_timeout` | Agent 空闲上限，0 表示关闭此监测 |
| `runtime.usage_tracking` | `auto`、`required` 或 `off` |
| `output.root` | 自动创建新运行目录的父目录 |

示例现在直接读取 **SkillsBench 原始任务** `workspace/dataset/skillsbench/tasks/paper-anonymizer`，使用其完整任务 Skill 包，源任务和输入快照均保持原字节。`prompt_augmenters` 根据 harness 选择增加器，当前 OpenHands/reference 从 [英文模板](prompts/execution/openhands_reference.md) 生成与原 reference 实验一致的单句提示：

```text
Before solving the task, invoke and read these task-provided Skills: `academic-pdf-redaction`, `pdf`.
```

随后拼接原任务正文，通过 BenchFlow 的 `prompts` 参数传入，不写回 `task.md`。此句请求调用任务提供的 Skill，不新增步骤适用性或任务优先级规则。Skill 是否实际读取，仍由 `backend/skill_reads.json` 根据工具返回的完整正文与来源审计；不宣称禁用了 OpenHands 的内置 Skill。

`explicit_skill_read=false` 或 `skill_mode=no-skill` 时不增加提示。启用增加器但 harness 未实现时，在启动前明确拒绝；后续 harness 应增加独立实现，不能套用 OpenHands 的提示。相同 reference 前缀重复输入时保持幂等；冲突前缀或旧通用长提示会报错，要求使用原始任务。目录名与 Skill 声明名称不一致时也明确拒绝，防止 reference 的目录名与 OpenHands 实际加载名不一致。当前支持普通单任务正文，原生 `Prompt/Role/Scene/User-persona` 特殊区段尚未接入增加器。

在准备出的目录查看 `prompt/effective.md` 即可检查最终提示，`prompt/manifest.json` 记录 harness、reference 变体、模板哈希、任务和 Skill 版本以及三个文本文件的哈希。普通运行与 live worker 读取同一策略并核对已保存内容。旧实验保留当时的双层提示记录，与新的单句 reference 条件应分别分析。原始任务预览验收见 [检查记录](checks/20261012-openhands-reference/verification.json)。

任务包当前要求 native `task.md`、`environment/Dockerfile`、`verifier/test.sh`，`with-skill` 还需要 Skill 包。首版明确拒绝符号链接和特殊文件。副本采用独立文件，不使用硬链接。

## 产物

```text
runs/<run-id>/
  request.json                 # 本次请求，不含凭据值
  runtime_environment.json     # 版本及允许记录的提供商设置
  result.json                  # 统一结果，包括执行状态和独立评分状态
  redaction.json               # 已知环境凭据的脱敏记录
  input/
    manifest.json              # 来源、完整任务和 Skill 文件哈希
    tasks/<task-id>/            # 本次输入的独立快照
  prompt/                      # 启用显式提示时由 prepare/run 生成
    original.md                # 去除元数据后的原任务正文
    added.md                   # harness 对应的新增提示
    effective.md               # 实际传给执行 Agent 的完整 prompt
    manifest.json              # harness/模板/输入版本与文本哈希
  backend/
    config.json                # BenchFlow 实际输入，JSON 是合法 YAML
    command.json               # 命令参数和工作目录
    version.json
    stdout.log
    stderr.log
    events.jsonl               # T000001 等证据编号、原始行号、完整事件对象
  jobs/                        # 仅本次产生的 BenchFlow 原始运行产物
```

`result.json` 包含：

- `prepared`：实际提交给后端的任务／Skill 快照及版本摘要。
- `execution`：BenchFlow 版本、退出状态、后端原始结果、轨迹位置、事件数及完整性。
- `feedback`：benchmark 原始指标、可确定时的通过状态、CTRF 测试细节及原始反馈位置。
- `diagnosis`：独立的诊断状态、报告位置或错误；诊断失败不改写原评分。明确成功时记录 `status=skipped`、`reason=task_succeeded`，不视为诊断失败；其他未完成情况使 CLI 返回非零。

`status=completed` 表示执行与结果收集完成，任务仍可能 `passed=false`。缺失反馈不会变成 reward 0；未知的轨迹完整性为 `null`，不能推断未记录的步骤未执行。轨迹规范化保留原始事件内容（含非文本内容），只增加证据编号和来源位置，不提前生成诊断结论。

一次执行预期恰好产生一个 rollout；多个结果、任务名称不匹配、轨迹解析失败等均显式报告。环境、Agent、验证器和产物错误分别记录；验证器的原始分数与错误信息保留，不用 Agent 自检替代。

运行目录以私有权限创建。凭据只通过进程环境传递，`--env-file` 支持字面量 `KEY=VALUE` 和引号，不执行 shell 展开，已有非空环境变量优先。后端退出后，对采集的文本产物进行已知凭据值脱敏；源输入快照保持原字节以便核验哈希。未知的新凭据无法仅凭此机制识别，未经检查不应发布原始产物。

BenchFlow 的采集代理将 `DEBUG` 解释为布尔值。若宿主中该变量是 `release` 等非法布尔值，运行器只从子进程环境副本移除它，并记录 `backend/environment_filter.json`，宿主环境不变。`usage_tracking=auto` 允许采集失败时继续直连；若后续实验要求必须有模型调用计量，应选择 `required`。首次运行发现的问题与实际缺失项见 [运行器验收.md](运行器验收.md)，不能把不可用的用量占位值当作实测消耗。

总超时或中断时先发送 SIGINT，让 BenchFlow 执行正常清理，再按宽限升级为 SIGTERM／SIGKILL，并尽可能保存部分证据。Docker 后端还会根据本次 rollout 身份、Compose 标签及本次日志挂载核对容器，仅删除确认属于本次运行的容器并保存 `backend/cleanup.json`。无法确定身份或远程沙箱清理状态时记录 `cleanup_unconfirmed`，不猜测删除范围。定向清理不删除镜像、卷和网络；正常结束由 BenchFlow 负责清理。

## 扩展和验证

执行、适配和诊断通过以下接口连接：

- `BenchmarkAdapter.prepare / evaluate / collect_evidence`：任务准备、评分语义与原始记录到统一证据的转换。
- `PromptAugmenter.augment`：按 harness 生成显式调用提示；与 benchmark 数据转换分开，当前实现 OpenHands/reference。
- `RunBackend.run`：环境、Agent、验证器的运行与轨迹收集；声明是否支持验证后的环境访问。
- `VerifiedSession` / `VerifiedCallback`：后端在验证后提供执行记录与检查工具，等待诊断结束后清理。
- `InspectionTools`：目录、文本、PDF、图片等检查能力；Stage1 不接收具体框架的 SDK 对象。

目前真实接入 `SkillsBenchAdapter + BenchFlowBackend`。新增 benchmark 实现适配器；需要其他原生执行框架时再添加后端。新适配器在 `adapters/registry.py` 的选择层注册；进程内调用也可直接注入适配器。使用子进程后端时，注册必须在该工作进程内可用。测试中的替代适配器验证接口隔离，不代表已经真实验证了第二个 benchmark。Stage 1 的输入收集扩展边界见下节。

运行测试：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

测试使用临时任务和模拟后端进程，覆盖副本隔离、哈希、评分缺失／矛盾、重复运行拒绝、轨迹缺损、超时和中断。真实任务验收与这些工程测试分开记录。

## Stage 1 实时诊断

示例配置启用 `runtime.diagnosis_config = "discovery.toml"`。一次 `run` 自动完成：

**执行 → 验证 → 保存验证检查点 → 判断是否成功。**

- 有效评分且 reward = 1：记录 `task_succeeded`，跳过诊断后清理。
- 有效评分且 reward != 1：独立诊断 Agent 检查原环境，保存报告后清理，包括部分得分任务。
- 评分缺失、评分状态无效或 reward 无效：单独记录并停止自动诊断，不能当作 0 分；可归档的无效评分记录跳过原因为 `reward_unavailable`。

按 2026-10-14 确认的研究范围，自动 Stage1 与 Stage2 使用同一 `reward_eligibility` 条件：`feedback.status=scored`、`metrics.reward` 为有限数值，且 reward 不等于 1 时可进入；布尔值、数字字符串及缺失值不算有效 reward。`passed` 字段继续保留 benchmark 原始语义，不决定研究入口。新增 benchmark 的 adapter 应提供这一约定下的 reward，同时保留自身指标。跳过分支记录零模型和工具调用，不创建 `discovery/live/`。执行前哈希与原轨迹、评分仍正常保存。此规则适用于自动实时诊断入口；存档分析命令需显式调用。历史运行和报告保留当时的条件。

`backend/verified_record.json` 标记 `verified_at`，不会提前填写整次运行的 `finished_at`。评分、验证反馈和原始轨迹在 `backend/frozen_verification/` 保留副本及哈希。BenchFlow 清理后可更新最终遥测，因此不能要求最终 trial `result.json` 与清理前文件整体相同；应分别检查原评分和轨迹。

独立诊断 Agent 使用新会话，接收 Q／S／T／F 证据及资源目录，按需要调用 7 个读取工具：目录分页、文本读取、文本搜索、PDF 信息、PDF 分页提取、PDF 搜索、页面或图片预览。工具只接受目录中的资源 ID 和范围参数。实际观察编号为 `A000001` 等，保存原文件、观察文本、版本哈希、时间、图片和工具参数。图片按 [DeepSeek Chat Completions 的用户消息要求](https://api-docs.deepseek.com/guides/vision/) 附在同批工具回复之后，保留调用和证据对应关系。

执行前保存有界文件哈希；只有观察内容与初始哈希相同，才标为 `availability=initial`，这仍不证明执行 Agent 读过它。已知改变的文件为 `post_execution`，无初始哈希为 `unknown`；后两者不能作为当时适用条件的证据。执行行为仍必须引用原 T 事件。环境检查限制、超限、权限错误和证据不足均保留。

目录只标记是否记录了初始哈希，以及 `availability_check=pending_observation`，避免把尚未检查的文件误当作无法检查。实际可用性由读取后的哈希比较确定。

检查范围是任务工作区与提供的 Skill 目录；默认排除已知私密和隐藏评测路径、符号链接。新增 benchmark 可通过检查器的 `excluded_paths` 声明额外排除位置。当前容器检查实现需要 `python3`；其他环境可以实现自己的 `InspectionTools`。PDF 依赖容器已有的 PyMuPDF 或 Poppler；预览普通图片依赖 PyMuPDF；缺失依赖会记录为能力缺口。该范围不代表读取了整个容器的所有文件。

BenchFlow 验证器会收紧工作区权限。因此，执行前的文件扫描仍使用原任务用户，验证后的固定只读工具使用容器 `root` 读取保留文件，不修改目录权限。模型不能提交任意命令或路径。权限和原因保存为 `backend/inspection_access.json`；验证后能够读取某文件，不代表原执行 Agent 当时有权访问它。

当前实时诊断启用 `time_budget_only = true`：一次诊断最多 1800 秒，模型回合、工具调用和报告纠错不设次数上限；完成合法报告即可提前结束。实际调用次数仍逐次记录，有效次数上限在日志中记为 `null`。输入长度和单次 HTTP 请求时限继续生效，HTTP、网络和凭据错误不自动重试。程序不执行正文中的工具调用标记，也不自动改写引用；最终报告仍须通过原有校验。超时会停止后续回合，不能撤销已经发送的远端模型请求。旧配置未启用该开关时仍沿用 `max_rounds`、`max_tool_calls`、`max_report_repairs` 的计数预算，历史冻结实验不改写。后端整体 `runtime.timeout_sec` 仍覆盖环境准备、执行、验证和诊断。

工具调用轮同样请求 JSON 输出模式，但提供方返回不合协议的正文时仍可能无法解析，须经过上述校验与纠正。工具与 JSON 模式的组合通过过独立小请求检查；这不保证长任务中的每次响应均有效。

`evidence_views.py` 为实时诊断提供与 benchmark 无关的证据视图。原始轨迹、反馈和检查观察完整保留；展示文本只移除终端转义序列，不改动换行、缩进、行号或 Skill 源文本。`normalization.json` 记录每条变更的原文/视图哈希与原文移除区间，引用仍必须精确匹配视图原文。清洗后仍不匹配的引用需要模型纠正，不能用模糊匹配替代。

`evidence_repair.py` 为实时 Stage1 和 Stage2／Stage3 证据会话提供有界纠错上下文。引用失败时按本次声明的运行集合和证据编号，重显已读的原文片段，保留 `+1|` 等前缀、缩进与换行；未读部分仅提供 `read_evidence` 的分页参数，实际读取仍消耗工具预算。同名编号不能跨 source／baseline／candidate 混用。完整纠错消息保存在 `calls/<n>/repair_feedback.json`。程序不改写引用、不代替模型判断；修正后的完整输出重新经过原有校验。

完整文本能放入预留的上下文预算时直接展示；较大的输入提供明确标注的索引和部分摘录，完整内容通过 `read_evidence`、`search_evidence`、`read_evidence_index` 按原 Q/S/T/F/A ID 访问。这些工具只访问本次已收集证据，不能读取任意路径。证据读取同样消耗工具额度。报告引用必须来自本会话展示过的文字；轨迹文字未完整展示时，不能提交穷尽性的 `no_supported_conflict`，应保留 `insufficient_evidence` 或提交有证据支持的候选冲突并说明覆盖缺口。对话积累超过输入上限时，在完整工具调用批次结束后保存旧窗口并重建有界上下文；原始证据和检查记录继续保留。字符预算仍是本地保护值，不等同于提供方 token 上限。

```text
backend/
  initial_files.json           # 执行前文件版本及扫描限制
  verified_execution.json     # 验证完成时的后端记录
  verified_record.json        # 验证检查点；不是整次运行结束记录
  evidence_freeze.json        # 原评分、反馈与轨迹副本的哈希
  frozen_verification/
  skill_reads.json            # 实际 Skill 内容与来源审计
  inspection_access.json      # 检查用户、访问原因及证据限制
  live_discovery.json         # 独立诊断状态
  lifecycle_cleanup.json      # 清理尝试的起止时间
discovery/live/
  initial_input.json / input.json
  raw_evidence.json           # 完整原始文本，不覆盖原执行记录
  normalization.json          # 视图转换哈希与原文移除区间
  evidence_access.json        # 已展示的证据字符范围与覆盖情况
  catalog.json / tools.json
  prompt.md / runtime_prompts.json / config.json
  messages.json / tool_calls.jsonl
  calls/001/                  # 每轮请求、响应及用量
                              # 诊断输出与本轮 validation.json
  repairs.jsonl               # 纠正原因、轮次与是否还有额度
  context_windows/            # 有界上下文重建前的完整窗口
  artifacts/ / observations/  # 实际检查过的原文件、图片、A 证据
  validation.json
  report.json / report.md
  result.json
```

`stages/live_discovery.py` 只依赖统一证据和工具协议；`integration.py` 负责选择 adapter 和组装回调；`runners/benchflow_worker.py` 是当前 BenchFlow 0.6.3 生命周期接线。未修改第三方 BenchFlow。新增 benchmark 需要实现自己的记录转换；如采用其他执行框架，还需提供验证后环境访问。模拟接口检查不代表第二个真实 benchmark 已获验证。

此前真实运行完成了诊断与清理，但自动报告存在语义漏报，记录见 [实时诊断验收](Stage1实时诊断验收.md) 与 [独立语义复核](Stage1实时诊断语义复核.md)。2026-10-12 的 10 项新运行又暴露了输出协议、引用格式、输入大小及报告状态一致性问题；上述实时诊断修复已接入，尚需新执行验证模型的实际纠正能力。工程校验通过不代表诊断语义正确。存档文本诊断入口仍采用单次文本调用，其大小保护和失败留档规则不变。

### Stage 1 报告格式

诊断只研究 Skill 要求与实际执行轨迹之间的偏差。先判断所选条款在当时是否适用，再检查行为：适用但未执行或未落实明确要求为 `under_use`；不适用却执行相应动作为 `misuse`。普通代码错误不能自动归类，必须有具体条款和行为证据。Skill 之间或内部条款之间的文字矛盾不作为候选，也不放入 `other_findings` 进行归因；影响适用性判断的歧义记入 `coverage_notes`。任务要求和环境证据仍用于判断条款是否适用。存档与实时诊断共用 `prompts/discovery.md` 中的判定表、分类例子和输出要求；这些是模型提示约束，程序校验仍不保证语义正确。

诊断模型只需填写顶层 `status`、`summary`、`conflicts`。`status` 保留三种值：`conflicts_found` 表示有候选冲突；`no_supported_conflict` 表示检查后未找到支持的冲突；`insufficient_evidence` 表示信息不足，无法判断。只有第一种状态允许非空 `conflicts`，供 Stage 2 选择候选。

每个冲突只要求五个字段：

- `type`：`under_use` 或 `misuse`。
- `target`：`scope`、`skill_ref`、`quote`，定位整个 Skill 或具体子步骤。
- `diagnosis`：一段英文，合并说明适用条件、实际与预期行为的差异、验证反馈支持什么及仍有哪些限制。
- `decision_evidence`：当时适用条件的原文引用。
- `behavior_evidence`：执行行为的原文引用，至少包含一条轨迹证据。

冲突中的 `feedback_evidence`、`uncertainty`，以及顶层的 `other_findings`、`coverage_notes` 可在无内容时省略，程序补为 `[]`。有相关验证反馈时应引用；缺少反馈支持时必须在诊断中说明。输入包含限制或轨迹完整性未知／不完整时，`coverage_notes` 仍必须非空。可选字段不代表可以忽略反证、检查缺口或不确定性。

引用仍使用 `{"evidence_id": "T000001", "quote": "原文片段"}`，必须精确匹配对应证据；决策、行为、反馈三类引用的来源限制与实时可见性校验保持有效。`availability` 不明或执行后变化的文件不能证明当时的适用条件。

保存的 `report.json` 使用 `schema_version=2`。程序补齐运行 ID、输入摘要、候选语义状态，以及目标 Skill 的身份、路径和版本。使用新格式的模型输出时，冲突按顺序编号为 `C000001` 等，并根据每个冲突引用的所有轨迹证据计算 `trajectory_range`；兼容旧格式时保留原编号和合法跨度。这个范围仅表示引用跨度，不证明区间被完整检查，也不能单独证明漏用。Markdown 报告展示同一份诊断和证据。

Stage 2 仍可读取历史 v1 报告，保留其冲突 ID 并重新校验原始证据绑定；历史文件不改写。格式简化减少模型重复填写字段，未增加对诊断推理正确性的独立保证。

## Stage 2：候选补丁生成与验证

87 个任务的 Stage1 + Stage2 联合运行使用独立配置 `configs/stage1-stage2-full/`，由 `configs/reference-full/` 派生并启用实时诊断。启动命令：

```bash
cd /home/lihanlun123/skillpatch
CONCURRENCY=4 bash scripts/run_stage1_stage2_full.sh
```

仅检查配置可追加 `--check`，不执行任务或模型调用。默认读取 `.env.deepseek-v4-flash`，可通过 `ENV_FILE=/absolute/path` 指定其他文件；启动脚本清除旧的 `DEEPSEEK_API_KEY` shell 导出，使选定文件中的 key 生效。每次创建新的 `runs/stage1-stage2-full-*/`，保存配置副本、各阶段日志、退出码、逐任务流水线状态和 `summary.json`。

并发额度覆盖整条任务流程：每个槽位先新执行一个任务，有效评分且 reward != 1 时完成 Stage1，再对诊断中的每条冲突依次执行完整 Stage2。reward = 1、诊断无冲突或评分无效时不进入 Stage2。部分得分且 `passed=null` 的任务可以进入。每个冲突的补丁独立验证；此脚本不调用 Stage3。执行完成及证据完整性检查继续有效；单个任务执行/诊断或 Stage2 工程异常会记录并继续其他任务。补丁正常被拒绝不是工程异常。若存在工程异常，最终 `xargs` 退出码可能为 123，具体进度与决定以汇总和各阶段记录为准。

入口选择已完成、有效评分且 reward != 1 的运行中的一个 Stage1 冲突。零冲突、reward = 1、评分无效、引用不匹配、版本变化或证据被修改时拒绝进入。旧版文本输入先按其原存档结构核验摘要，再补齐新可选字段；这不重新证明诊断语义正确。

实时 Stage1 为阅读清理过终端控制字符时，Stage2 从原始记录按同一规则重建文本与转换元数据，再逐项核对；原文哈希、转换范围、证据来源及正文仍需一致。历史原始文本报告也可读取，原存档不会被改写。

先预检来源与执行预算，不调用模型或执行任务：

```bash
PYTHONPATH=src python3 -m skillpatch stage2 \
  --diagnosis-dir runs/<失败运行>/discovery/<诊断目录> \
  --conflict-id <冲突ID> \
  --config configs/stage2.toml \
  --prepare-only
```

完整闭环使用相同参数，去掉 `--prepare-only` 并传入 `--env-file`。`--generate-only` 仅生成候选及独立 Skill 副本，不启动任务，结果为 `unvalidated`。每次必须使用新输出目录；默认保存在来源运行的 `stage2/<episode-id>/`。CLI 返回 0 表示本轮处理正常完成或预检完成；是否接纳看 `decision` 和 `ready_for_stage3`。

复用已有补丁测试新的交付方式时，追加 `--patch-file /absolute/path/to/generation/patch.json`。程序核对补丁与原诊断的完整绑定，保留补丁内容和 revision，只重新运行候选并独立评价；不调用生成模型，不消耗或重置生成次数，也不自动修订补丁。此模式仍要求新输出目录，原执行、原补丁与原验证记录保留。

配置见 [configs/stage2.toml](configs/stage2.toml)。生成与独立评价使用各自的模型配置和英文 prompt，模型客户端及请求、原始响应、用量存档沿用现有接口。补丁正文只有英文 `when_to_use` 和 `how_to_interpret`；生成理由与评价说明也使用英文。补丁 ID、来源冲突、版本、锚点、修订号由程序绑定。

Stage2 与 Stage3 的评价输出统一为七个字段：`baseline_status`、`target_status`、`explanation`、`baseline_evidence`、`candidate_evidence`、`reverse_conflicts`、`revision_action`。按 2026-10-14 用户要求，删除 `uncertainties` 字段及其接纳拦截。证据不足以判断时，使用对应的 `uncertain` 状态，并在 `explanation` 中说明原因；引用校验、验证器比较和修订请求检查继续生效。Stage3 导入旧评价时在内存中忽略已废弃字段，历史文件及原始哈希不改写。

生成与评价通过证据目录和只读工具分批读取完整存档。首次输入优先展示任务要求、目标 Skill、验证反馈和执行轨迹；大型脚本、XML schema 及其他材料仍可搜索或分页读取。工具需要指定 `source`、`baseline` 或 `candidate` 集合，评价引用必须属于对应运行且实际展示给模型。源任务的 `source` 与 `baseline` 是同一次执行的两个逻辑角色；评价输入明确记录运行角色和复用关系，回归任务与历史记录的不同运行不能混用。上下文过大时保留存档并重建有界窗口，模型可再次读取所需段落。

Stage2 生成、评价及 Stage3 整合、保持效果评价按目标相关性读取轨迹。程序要求执行记录完整、行为判断具有真实轨迹证据、引文精确匹配对应运行中已展示的片段；不再强制从最早引用事件起逐条读到结尾，也不要求读完整条长事件才能引用其中已展示的片段。模型仍须搜索和检查相关的后续修正、撤销、等效实现、最终结果及反证；关键证据不足时保留待验证。展示记录用于核验引用访问，不保证模型的判断正确。

Stage2 当前生成配置启用 `time_budget_only = true`，每次补丁生成由 `generation_session_timeout_sec = 1800` 限时，模型回合、工具调用和输出纠错不设次数上限。评价保持原设置：`model_session_timeout_sec = 900`，最多 50 轮、100 次工具调用和 5 次纠错。生成专用时限省略时回落到 `model_session_timeout_sec`，兼容旧配置。两阶段的单次 HTTP 请求仍受各自 `timeout_sec` 限制。`plan.json` 记录两阶段各自有效时限；没有次数上限的字段为 `null`，实际调用次数继续记录。补丁修订次数和候选任务执行预算独立于模型会话预算，保持原设置；1800 秒是每次诊断或生成会话的上限，不是整个任务优化流程的合计时限。

补丁默认采用独立文件模式 `sidecar_v1`：在所属 Skill 目录新增 `SKILL_PATCHES.md` 和程序核验用的 `SKILL_PATCHES.json`，所有既有文件（包括 `SKILL.md`、YAML description、脚本和参考资料）逐字节保留。补丁文件记录目标相对路径、版本、精确引文及 `when_to_use` / `how_to_interpret`；不携带诊断报告、验证器答案或执行轨迹。Stage2 按文件版本及精确引文唯一匹配绑定目标，普通段落、表格行及标题连同代码块的目标均不依赖插入位置，也不要求归属某个标题或编号步骤；结构对象归属及同对象判断留在 Stage3。OpenHands 增强提示要求在应用 Skill 前显式读取同目录补丁，实际工具观察用于确认读取。无补丁时保持原 reference 提示。其他 harness 需要实现相应交付和读取机制。

历史内嵌模式标记为 `inline_v1`，缺少模式字段的旧记录按原方式重建核验；不会改写历史实验。Stage3 只有在呈现模式和实际文件均与已验证候选一致时才继承效果证据；从历史内嵌版本迁移到独立文件需重新验证。

按 2026-10-14 用户确认，源任务直接复用第一次执行作为 baseline，生成补丁后只新跑 candidate；基线证据和评分来自该次完整存档，不再重跑无补丁源任务。候选采用原任务初始快照、相同执行模型和预算，核验所有原文件及实际任务消息；任务提示仅允许新增经过记录和复核的补丁读取说明。源诊断仅供生成与评价，不送给执行 Agent。验证运行关闭普通 Stage1 自动诊断，由 Stage2 独立评价原问题，所以候选成功后仍要评价冲突是否修复。评价可按需读取完整存档文本与已存档的观察，未接入运行结束后的容器检查；缺失产物、图片或轨迹导致不能判断时必须待验证。

`regression_configs` 指定相关成功任务的运行 TOML；其目标 Skill 必须与来源使用同一版本，其余 Skill 和资源按该任务原包保留。配置的回归任务没有可直接复用的来源执行存档，仍新跑 baseline 和 candidate；每个回归任务的基线必须明确成功，否则不能作为有效回归证据。`repetitions` 对源任务表示多个新 candidate 与同一次 source baseline 比较，不能声称获得了多个独立基线；对回归任务表示相同次数的基线和候选。候选修订继续复用来源基线及本轮已执行的回归基线。第一个候选加最多两次修订；预算按来源冲突持久化，换目录不重置。调用失败或中断也保留已占用的尝试，模型 HTTP 请求不自动重试。

接受需要源任务各组都显示：来源基线确实存在目标冲突、候选修复目标、验证器客观改善，以及没有已观察到的反向使用冲突或结果退化。复用来源运行不代表自动认可 Stage1 诊断，独立评价仍须核实原问题；这一策略不提供无补丁重跑时的复现率。SkillsBench 由 adapter 比较整体 reward 和具名测试；reward 不变时，只有来源冲突反馈引用能定位的相关子项改善才支持局部修复，其他子项改善不足以证明目标改善。二值反馈前后都失败且仅模型声称修复时为 `pending`。

**按用户确认，未配置相关成功任务时，允许有效候选带回归覆盖缺口进入 Stage3。** `regression_coverage` 明确记录 `not_configured`、任务数和已检查组数。已配置但记录不完整的回归任务仍导致待验证；观察到退化则拒绝。模型要求重新检查诊断时记录 `next_action=return_to_stage1` 并停止本轮，不自动追加新的 Stage1 调用。

```text
stage2/<episode-id>/
  source.json / config.json / plan.json
  result.json                   # 处理状态、接纳结论与覆盖缺口
  cases/                        # 可选回归任务的预检副本
  baselines/                    # 可选回归任务的新基线；源任务引用 source 存档
  attempts/000/
    generation/                 # 独立会话、calls、collections、输出、patch.json
    packs/                      # 各任务的临时候选 Skill 包
    presentation/               # 补丁 diff、全包哈希与呈现限制
    runs/                       # 新候选运行
    evaluation/                 # 独立评价会话、三组证据、引用校验和结果
    validation.json             # 本候选证据及接纳结论
  stage3_queue.json              # 仅接纳后生成，绑定补丁、版本及验证哈希
```

生成 Stage3 队列不会自动改变当前补丁集合，需显式运行下述 `stage3` 命令。评分语义扩展实现 `BenchmarkAdapter.compare_feedback(..., target=...)`；其他执行框架可注入 backend factory。模拟的独立 table benchmark 已验证接口边界，不代表第二个真实 benchmark 已经验证。验收记录见 [Stage2验收.md](Stage2验收.md)。

## Stage 3：补丁集合整合与版本管理

`stage3` 接收一个已接纳的 Stage2 队列。它重新核验原诊断、补丁正文、实际候选资源、基线／候选执行与独立评价，并重算接纳结论；支持核验源运行复用为 Stage2 基线的记录，只写着 `accepted` 的文件不能直接进入集合。Stage3 自身的新组合验证及整合保持效果对照策略不变。历史报告和 Stage2 预算保持原样。

首次登记需提供明确的 `--skill-key`，例如 `skillsbench/reference/xlsx`。程序绑定该 Skill 的完整原始资源摘要，再按 whole_skill／substep 及原始结构边界产生稳定 `target_id`。同一步的不同引文可以对应同一对象；同名文件、相似补丁文字不会自动归为同一对象。定位不唯一、跨越多个边界或来源版本未知时保留待处理结果。历史报告的 `target.version` 仍表示当时部署的版本，新映射单独保存在 `target_binding.json`。

预检不会初始化注册库、切换版本、调用模型或运行任务：

```bash
PYTHONPATH=src python3 -m skillpatch stage3 \
  --queue runs/<来源任务>/stage2/<已接纳轮次>/stage3_queue.json \
  --skill-key skillsbench/reference/xlsx \
  --registry patch_sets \
  --config configs/stage3.toml \
  --prepare-only
```

正式处理去掉 `--prepare-only`。若需要模型或任务复验，传入原实验的 `--env-file`。`--original-skill` 可显式指定单个原始 Skill 的资源目录；未指定时，首次登记使用来源任务中的目标 Skill，之后使用已登记原始快照。每轮使用新的 `--output` 目录；默认产物写到来源任务的 `stage3/` 下。CLI 返回 0 表示本轮处理完成或预检完成，**是否接纳以 `result.json` 的 `decision` 为准**。

处理规则如下：

- 首条补丁在实际呈现与已验证候选逐文件一致、来源和执行条件通过核验时，沿用 Stage2 证据，无需强制改写或重复实验。
- 重复队列不重复处理；正文完全一致的补丁只关联证据，保留该证据原先验证的版本，不据此宣称新的跨任务效果。
- 已有集合中加入新内容时，先验证当前集合与加入新补丁后的组合，检查新来源问题是否修复及旧来源修复是否保留。旧版本队列同样不能直接生效。
- 同一对象已有不同补丁时，独立模型可以 `keep`、`merge` 或 `defer`。只有 `merge` 输出新的 `when_to_use` 和 `how_to_interpret`；它不能修改原始 Skill、脚本、对象身份或来源证据。
- 合并改变实际呈现后，对比未合并组合与合并集合，独立检查修复保持情况。评分持平可以接纳，但必须确认已有目标冲突未重现、候选保留修复，且没有已观察到的新使用冲突或退化。

来源任务取各补丁证据的并集；`regression_configs` 可补充相关成功任务，任务配置与来源资源均须核验。已配置的回归检查也适用于纯登记或去重路径。缺失证据、执行失败或评价不确定时待验证；观察到退化时拒绝。未配置成功任务时，仍允许带 `regression_coverage=not_configured` 进入集合，不能据此声称跨任务泛化。

[configs/stage3.toml](configs/stage3.toml) 分别指定整合模型、保持效果评价及新增组合修复评价。每轮最多一次整合建议、两阶段成对任务验证；每个来源／回归任务按 `repetitions` 运行。`max_validation_cases` 在启动任务前限制任务并集，具体上限写入 `plan.json`。整合与保持效果评价会话默认 50 轮、100 次工具调用、2 次格式纠错、900 秒总时限；新增组合的修复评价复用 Stage2 评价配置（当前 5 次输出纠错），不重置 Stage2 的生成预算。

```text
stage3/<episode-id>/
  plan.json / result.json       # 预算、决策、覆盖范围、是否切换版本
  target_binding.json           # 历史引用到原始对象的独立映射
  incoming.json / receipts/     # 补丁与已核验来源的哈希清单
  proposal/                    # 按需生成整合建议及证据会话
  combination/                 # 按需验证新补丁组合
  preservation/ / coverage/    # 按需验证保持效果或额外回归覆盖
  selected_entries.json        # 最终选中的补丁条目及来源
  rendering.json / rendered/   # 原始资源、独立补丁及来源映射
patch_sets/<skill-identity>/
  base.json / original/        # 固定原始 Skill 与资源
  versions/<version-id>/       # 不覆盖的 manifest.json 和 skill/ 副本
  current.json / checkpoints/ # 生效指针及追加式切换记录
```

通过验证后才提交不可变版本并切换指针；提交时再次核对父版本，阻止并发旧结果覆盖新集合。失败或暂存保留上一版。查看与导出：

```bash
PYTHONPATH=src python3 -m skillpatch patch-set \
  --registry patch_sets --skill-key skillsbench/reference/xlsx show

PYTHONPATH=src python3 -m skillpatch patch-set \
  --registry patch_sets --skill-key skillsbench/reference/xlsx history

PYTHONPATH=src python3 -m skillpatch patch-set \
  --registry patch_sets --skill-key skillsbench/reference/xlsx \
  export --output <新的单个Skill目录>
```

回退使用同一入口的 `rollback --version <历史版本ID或original> --expected-parent <当前版本ID>`。回退到 `original` 创建一个空补丁集合版本；原始资源及历史版本都保留。导出的是单个 Skill，不能直接将它作为要求完整多 Skill 包的 `task.skills_dir`。

执行器显式选择补丁集合，并保留任务中其他 Skill 和资源：

```bash
PYTHONPATH=src python3 -m skillpatch prepare \
  --config configs/reference-full/exceltable-in-ppt.toml \
  --patch-registry patch_sets \
  --patch-skill-key skillsbench/reference/xlsx \
  --output <新的准备目录>
```

`prepare` 仅检查部署；实际实验改为 `run` 并提供环境文件。可加 `--patch-version <版本ID或original>` 固定版本，省略时在准备开始前固定当前版本。有多个相同资源的 Skill 时用 `--patch-skill-path xlsx/SKILL.md` 消除歧义，该参数仍要求完整资源摘要匹配。普通命令未传补丁参数时保持原配置。

部署准备在旁边创建 `<输出目录>-patch-input/`，记录选定版本、原任务快照与组合后的完整 Skill 包；执行目录的 `patch_set.json` 记录实际部署核验。后端启动前再次检查实际准备出的 Skill 包摘要，防止适配器忽略替换。各轮证据、注册库及部署准备目录应一起保留，后续整合仍会核查来源。

| 文件 | 职责 |
| --- | --- |
| `patch_targets.py` | 原始对象边界与稳定身份 |
| `patch_set_rendering.py` | 保留原始资源的补丁呈现及引文来源映射 |
| `stage3_sources.py` | 重新核验已接纳队列、执行证据与来源清单 |
| `stages/consolidation.py` | 去重、整合、验证和版本提交的总流程 |
| `stages/consolidation_model.py` | 按对象提出保留、合并或暂存建议 |
| `stage3_validation.py` / `stages/consolidation_evaluation.py` | 全新成对执行及保持修复的独立评价 |
| `patch_sets.py` | 不可变快照、生效指针、导出和回退 |
| `patch_deployment.py` | 经 adapter 将指定集合部署到任务副本 |

核心依赖统一 adapter、backend 和证据接口；目前真实 benchmark 仍只有 SkillsBench。第二种 benchmark 的工程替身测试用于验证接口隔离，不能替代真实适配实验。

## Stage 1 存档文本诊断

输入来自本轮真实执行产生的 Executor 记录；不会加载前期历史实验的散落轨迹。可以先检查组织后的证据，不调用模型：

```bash
PYTHONPATH=src python3 -m skillpatch diagnose \
  --run-dir runs/20261011T062354Z-paper-anonymizer-4d0aa0bf \
  --config configs/discovery.toml --prepare-only
```

实际诊断：

```bash
PYTHONPATH=src python3 -m skillpatch diagnose \
  --run-dir runs/20261011T062354Z-paper-anonymizer-4d0aa0bf \
  --config configs/discovery.toml \
  --env-file /home/lihanlun123/workspace/experiments/skillsbench-with-skill/.env.deepseek-v4-flash
```

`diagnose` 分析已经完成的这次运行，不重复启动执行 Agent 或验证器。每次创建新的 `discovery/<diagnosis-id>/`，也可用 `--output` 指定新目录。任务根目录的 `result.json` 保持原样。诊断模型独立配置为 `deepseek-flash`，非思考模式，单次 Chat Completions 请求；不通过执行 Agent 的框架调用。

| 文件 | 职责 |
| --- | --- |
| `models.py` | `DiscoveryInput`、`EvidenceItem`、`Conflict`、`DiscoveryReport` 等统一对象 |
| `context.py` / `adapters/skillsbench_context.py` | 公共证据呈现与摘要；SkillsBench 专属来源核验与 Q／S／T／F 收集 |
| `prompts/discovery.md` | 适用性与实际行为对照、Under-use／Misuse、证据不足和输出规则 |
| `model_client.py` | 独立模型配置、请求、原始响应、结束原因及用量记录 |
| `discovery_validation.py` | 输出结构、证据类型、原文引用、唯一目标和轨迹范围校验 |
| `stages/discovery.py` | 组织输入、调用、校验及 JSON／Markdown 报告保存 |

本项目维护的 prompt 统一使用英文，并集中放在 `prompts/`；后续新增的执行提示、阶段指令和工具使用说明也遵循此约定。新生成的 Stage1 报告、Stage2 补丁及理由、独立评价、Stage3 整合及保持效果说明、程序回退文本和 Markdown 报告标签均使用英文。新输出校验会拒绝自撰字段中的中文汉字并进入已有纠错流程；该检查不是完整的语言识别器。原始证据引用、benchmark 任务、Skill、轨迹、绑定路径和历史产物保持原文；不翻译或覆盖旧实验。违反语言要求的模型原始响应仍如实留档，不作为校验通过的最终产物。

模型看到实际任务消息、完整任务文本、可读取的 Skill 材料、全部可用文本事件和验证反馈。`Q` 是任务证据，`S` 是 Skill 原文，`T` 沿用运行器事件编号，`F` 是反馈。源文件的绝对路径保存在本地 `input.json`；提示词通过编号引用。oracle 和验证器源码正文不作为诊断输入。Skill 读取、行为执行和等效实现分别判断。

`adapters/skillsbench_context.py` 核验当前 SkillsBench／BenchFlow 的快照与 ACP 产物布局；公共 `context.py` 根据适配器选择相应收集器。其他 benchmark／后端可通过 `run_discovery(..., input_builder=...)` 提供相同的 `DiscoveryInput`；模型提示词、引用校验和报告代码无需改变。新采集器仍须实现自身的来源校验和完整性记录，不能将当前读取器称为已经通用支持任意 benchmark。

超过 `max_input_chars` 时显式失败，不自动截断。该值是字符预算，不是提供商 token 预算。存档文本模式不加载 PDF 或图片；图像占位符和缺失内容记录在 `limitations`。实时模式则以实际工具观察为准。`trajectory_complete=true` 仅表示事件记录完整，并不保证原始产物或视觉内容全部可见。

```text
discovery/<diagnosis-id>/
  input.json                  # 本次证据、出处、版本与输入摘要
  prompt.md                   # 实际系统提示词
  config.json                 # 模型参数；仅保存凭据变量名
  messages.json               # 实际完整模型消息
  request.json                # 请求体，不含 Authorization
  response.txt / response.json
  model_output.txt            # 未替换为人工修订的模型输出
  model_call.json             # 状态、模型、结束原因、真实用量与耗时
  validation.json             # 结构／引用检查结果，不验证语义真伪
  report.json / report.md     # 仅在本地校验通过后生成
  result.json                 # 本次诊断执行状态
```

API 失败、输出截断或引用不匹配会保留失败记录，不冒充“无冲突”，不自动重试。提示词修订后如需重试，执行新的诊断命令，两个版本均保留。`prepare-only` 不产生模型响应或冲突报告。

正式输出支持 `conflicts_found`、`no_supported_conflict`、`insufficient_evidence`。所有报告均标为 `candidate_diagnosis`：本地校验只能证明字段和引用有效，不能证明模型的适用性判断或归因成立。双类冲突的模拟测试也不能替代真实 Misuse 案例。首次调用与复核记录见 [Stage1验收.md](Stage1验收.md)。
