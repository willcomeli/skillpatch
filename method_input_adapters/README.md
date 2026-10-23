# 方法输入适配层

共享 SkillsBench、ALFWorld、ScienceWorld、AppWorld 执行器；保留原始执行输出，分别转换为各个方法的输入。本目录包含 SkillPatch、SkillOpt 和 Trace2Skill 的适配层。

```text
method_input_adapters/
├── skillpatch/    # 输出 DiagnosisContext
├── skillopt/      # 输出评分字典及 conversation.json，接入原始训练器
└── trace2skill/   # 输出完整日志，接入原始分析和演化流程
```

SkillPatch 的公共调用入口为 `from method_input_adapters.skillpatch import build_diagnosis_context`。

## 安装与命令

在项目根目录执行，使用独立 Python 环境：

```bash
uv venv .venvs/methods --python 3.12
uv pip install --python .venvs/methods/bin/python -r method_input_adapters/requirements.lock.txt
uv pip install --python .venvs/methods/bin/python --no-deps -e ./method/skillopt
.venvs/methods/bin/python -m method_input_adapters --help
```

依赖锁记录本次验证环境；三个方法的源代码继续保存在 `method/`。Trace2Skill 在调用时加载本地快照，不修改其源代码。测试命令：

```bash
.venvs/methods/bin/python -m unittest discover -s tests -q
```

## SkillOpt

将已有执行记录转换为上游实际读取的格式：

```bash
.venvs/methods/bin/python -m method_input_adapters skillopt-export \
  <SkillsBench运行目录或原生单任务JSON> \
  --output <新目录>
```

产物是 `results.jsonl`、`predictions/<id>/conversation.json` 和 `adapter_manifest.json`。`hard` 来自正式成功判定，`soft` 来自正式的 `[0,1]` 分数。评分缺失、无效或执行错误会报错；成功轨迹也保留。SkillsBench 保留原始事件所有字段；原生轨迹保留动作前观察、动作、模型返回的 thought、动作后反馈和逐步元数据。原生步骤不能被视为完整模型协议记录，限制随适配结果保存。

运行原始 `ReflACTTrainer`：

```bash
.venvs/methods/bin/python -m method_input_adapters skillopt-train \
  --manifest <任务清单.json> --initial-skill <初始Skill.md> \
  --output <新训练目录> --env-file <凭据文件>
```

默认是一轮小规模训练；`--config <SkillOpt YAML>` 可覆盖其训练、优化和评估参数。模型连接默认读取 `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL`，可用 `--api-key-env`、`--base-url-env`、`--model` 改变。目标模型、工具、执行预算等由清单中原有执行 TOML 决定。所有 rollout，包括候选验证、slow update、meta update 和测试，均回到共享执行器；输出目录必须全新，不自动复用旧结果。

任务清单格式（路径相对清单文件）：

```json
[
  {"id":"train-1","split":"train","config":"train-run.toml","skill_target":"pdf/SKILL.md"},
  {"id":"val-1","split":"val","config":"val-run.toml","skill_target":"pdf/SKILL.md"},
  {"id":"test-1","split":"test","config":"test-run.toml","skill_target":"pdf/SKILL.md"}
]
```

原生数据集可额外指定 `task_ref`，结构与原 Runner 的任务引用相同；若配置只包含一个任务则自动读取。清单 ID 唯一，任务不得跨集合重复。SkillOpt 的 `valid_seen` 映射到 `val`，`valid_unseen` 映射到 `test`。默认不运行测试；启用 `eval_test` 时必须提供独立测试任务。

SkillsBench 复制整个原 Skill pack，仅替换指定 `skill_target` 的正文，保留辅助文件和其他 Skill；多 Skill pack 必须明确指定目标。SkillOpt 本身更新一个 Skill 文本。需要每个方法都使用相同初始 Skill、任务划分和目标执行配置，才能比较效果。

## Trace2Skill

```bash
.venvs/methods/bin/python -m method_input_adapters trace2skill-export \
  <运行目录或原生单任务JSON> --output <新目录>

.venvs/methods/bin/python -m method_input_adapters trace2skill-evolve \
  <运行目录或原生单任务JSON> \
  --initial-skill-dir <含SKILL.md的目录> \
  --output <新目录> --env-file <凭据文件>
```

`trace2skill-export` 生成带 `_SUCCEED.md` / `_FAILED.md` 后缀的完整日志和来源清单。不截断轨迹。不同执行记录可以来自同一任务，实例 ID 同时区分源路径；不会覆盖不同轮次的日志。

`trace2skill-evolve` 使用上游已有的纯文本分析分支：完整日志 → 原始成功/失败分析器 → 原始解析器 → `CombinedParallelSkillEvolver` 的 MAP / REDUCE / TRANSLATE / APPLY。只替换表格领域假设，保留分析和修改的结构。实际分析 prompt 和响应会保存；不复用 SkillPatch 的诊断报告。它与会检查表格工作文件的 agentic 分支不同。

演化请求的输出预算默认 32768 tokens，可用 `--max-output-tokens` 调整；分析报告请求为 8192 tokens。输入轨迹不截断。复杂 Skill 的结构化修改可能超过较小的输出预算，上游会尝试继续生成；最终未解析出修改时不能视为生成了候选修改。

两套方法的优化模型请求默认超时 1800 秒；这是单次请求上限，不是整个训练实验或基准执行的总时限。

初始 Skill 整个复制到输出的 `skill/`，保留参考文件和受保护资源；只允许修改 `SKILL.md` 与 `references/` 文档，原目录保持原样。`parsed_*_records.json`、`analysis/`、`evolution/`、`evolution_summary.json`、`result.json` 保存实际过程。

加 `--validation-manifest <仅包含val任务的清单>`，会分别用初始 Skill 和候选 Skill 调用共享执行器，保存 `validation.json`；验证任务与输入轨迹任务不得重合。不指定此参数时只完成分析和生成，`validation_status=not_requested`，不能解释为任务评分验证通过。验证结果不会自动激活候选。

SkillsBench 交付完整 Skill 目录并保留其他 Skill。三个原生 Runner 目前只有文本 Skill bank，因此会将 `.md` / `.txt` 参考文档连同相对名称附在正文后；这是全文注入，不提供按路径访问的文件系统。包含可执行脚本或二进制资源的目录会明确拒绝，不能声称与原生文件工具等价。

上游快照没有 `skills/skill-creator/scripts/quick_validate.py`，其内部结构验证会跳过。此状态明确记录在 `result.json`；适配层另行检查输出非空、路径边界和受保护文件完整性。真实任务是否改善仍需正式执行验证。
