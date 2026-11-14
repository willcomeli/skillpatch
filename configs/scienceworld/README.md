# ScienceWorld 实验

从仓库根目录执行。三组方案使用同一执行 Agent 和同一份 test 清单。普通 SkillPatch 与 Boundary-Aware 还共享 Stage 1 诊断及来源任务回放清单。

## 安装和配置

本地执行需要 Python 3.12、`scienceworld==1.2.3`、`requests` 和 Java。node01 的实验环境使用 OpenJDK 11。方法阶段默认使用 OpenHands，安装和本地修补步骤见 [仓库 README](../../README.md)。Vanilla 只需要执行环境，ScienceWorld 的两种 SkillPatch 路径无需安装 SkillOpt 或 PyYAML。

```bash
uv venv .venvs/methods --python 3.12
uv pip install --python .venvs/methods/bin/python scienceworld==1.2.3 requests
cp configs/scienceworld/scienceworld.env.example configs/scienceworld/scienceworld.env
cp configs/scienceworld/scienceworld_evolve.env.example configs/scienceworld/scienceworld_evolve.env
```

在对应文件里填写模型地址和密钥。系统环境中已设置的值优先。由 [experiment.toml](experiment.toml) 汇总所用文件，换实验时可传入 `--experiment`。

| 阶段 | Agent 和模型 | 提示词来源 |
|---|---|---|
| 采轨迹、回放、测试 | Native `ScienceWorldActionAgent`，`scienceworld.env` 的 `EXECUTION_MODEL` | [ScienceWorld 动作提示词](../../prompts/execution/scienceworld_action.md) |
| Stage 1，两个策略共用 | `AgentClient`，默认 OpenHands，`scienceworld_evolve.env` 的 `METHOD_MODEL` | `evolution.toml` 的 `[stage1]` |
| 普通版 Stage 2、3 | 同一个方法 Agent 配置 | `[stage2]`、`[stage3]` |
| Boundary-Aware Stage 2、3 | 同一个方法 Agent 配置 | `[boundary_aware.stage2]` 的 Inductor / Proposer，`[boundary_aware.stage3]` 的整合提示词 |

两份 env 样例记录本次实验的模型名，执行为 `deepseek-flash`，方法为 `openai/gpt-5.6-sol`。所有 Stage 1–3 提示词路径在 [evolution.toml](evolution.toml) 中。下面的命令显示实际解析到的模型、harness、提示词与摘要及任务清单。

```bash
.venvs/methods/bin/python -m set_up.cli scienceworld describe
```

[execution.toml](execution.toml) 设置原始 Skill、自由动作、8 步历史、单次模型输出上限 4096 和环境步数上限 100。训练清单中的 `recommended_max_steps` 可以覆盖 Agent 的任务步数。默认使用 [ScienceWorld 分集](../../data/scienceworld/README.md) 的 train 96 条和 test 211 条。数据来自固定公开索引的重建，重建方式及与 EviSkill 包装文件的已知差异见 [来源核对](../../data/scienceworld/SOURCE_AUDIT.md)。

## 自动运行

```bash
scripts/scienceworld/run.sh runs/scienceworld/reproduction --method all
```

`--method` 可选 `standard`、`boundary-aware`、`vanilla` 或 `all`。默认执行全部三组。使用相同参数和输出目录续跑。可用 `--train-run DIR` 复用既有训练批次，可重复传入历史补跑目录。用 `--issues FILE` 从已有的 Stage 1 问题索引开始，也接受旧版 ScienceWorld Stage 2 计划。脚本按顺序执行所选方法，三组对比写到输出根目录的 `test_comparison.json`。

## 分步运行

```bash
py=.venvs/methods/bin/python
run=runs/scienceworld/reproduction

"$py" -m set_up.cli scienceworld collect --output "$run/train"
"$py" -m set_up.cli scienceworld diagnose --train-run "$run/train" --output "$run/stage1"
"$py" -m set_up.cli scienceworld evolve --issues "$run/stage1/issues.json" \
  --induction standard --output "$run/standard"
"$py" -m set_up.cli scienceworld evolve --issues "$run/stage1/issues.json" \
  --induction boundary-aware --output "$run/boundary-aware"

"$py" -m set_up.cli scienceworld evaluate --output "$run/vanilla"
"$py" -m set_up.cli scienceworld evaluate --stage3-run "$run/standard/stage3" \
  --output "$run/standard/evaluation" --server-port-start 8911
"$py" -m set_up.cli scienceworld evaluate --stage3-run "$run/boundary-aware/stage3" \
  --output "$run/boundary-aware/evaluation" --server-port-start 9011

"$py" -m set_up.cli scienceworld compare --vanilla "$run/vanilla" \
  --standard "$run/standard/evaluation" --boundary-aware "$run/boundary-aware/evaluation" \
  --output "$run/test_comparison.json"
```

- `collect` 把计分轨迹和续跑位置保存到 `train/result.json`。用 `--train-manifest` 可选择带 `split = "train"` 的子集。
- `diagnose` 读取原始任务结果，归档 Agent 的报告，并经现有校验器校验结构和引用。通过校验且已有 Skill 锚点的条目进入 `issues.json`。
- `evolve` 在同一个问题索引上生成普通版或 Boundary-Aware 候选。同一目标的多个候选交给 Stage 3 提案，单候选直接打包。产物包括 `stage2/candidates.json`、`stage3/result.json`、`stage3/bundle_receipt.json` 和 `stage3/candidate_skill/`。
- `evaluate` 有补丁包时默认依次回放和测试；Vanilla 只测试。可用 `--phase replay` 或 `--phase test` 单独执行。用 `--prepare-only` 核对评测输入和补丁交付。跳过的任务保留在待计分清单，续跑只处理缺失任务。评分写到 `patched/replay/result.json`、`patched/test/result.json` 或 `initial/test/result.json`。
- `compare` 核对任务清单、执行模型和原 Skill 摘要，以相同 task ID 配对。未计分的任务列在 `missing`，部分结果会标为 `incomplete`。

只调试单条报告或候选时，使用 `set_up.cli stage1`、`stage2`、`stage3`。三个命令都可传入 `configs/scienceworld/evolution.toml` 和同一个 `--env-file`。单条 `stage3` 的输出是整合提案，批量 `scienceworld evolve` 负责将提案交付为可执行补丁包。

## 后台运行

可以在服务器的 tmux 中运行整个脚本。已有补丁包且需要同一评测目录里每小时看一次进度时，可使用 `set_up.cli scienceworld supervise --stage3-run DIR --output DIR`。它调用同一 CLI 的两个评测命令，状态写到 `status.json` 和 `checks.jsonl`。
