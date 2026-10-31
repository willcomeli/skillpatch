# AppWorld 全流程连通测试与单轮改进

三个方法共用同一份初始 Skill 和第一轮训练轨迹。采集一次后，重复使用同一个 `trajectory_manifest.json`。全流程连通测试覆盖执行器存档、三种方法的真实改进、候选交付、改进后的执行和官方评分。通过条件是流程完成，不要求提高得分。SkillPatch 不运行 dev；连通测试会检查 Trace2Skill 和 SkillOpt 的 dev 执行接口；正式实验只有 SkillOpt 使用 dev 门控。

所有命令在项目根目录运行。脚本使用 `.venvs/methods/bin/python`，可通过 `SKILLPATCH_PYTHON` 指定其他解释器。默认读取根目录 `.env`，可传 `--env-file`；加载器沿用已有规则：非空进程环境变量优先于文件。同一输出目录不可复用，省略 `--output` 会在 `runs/appworld/` 创建唯一目录。

## 执行模型与方法模型

统一的 `smoke.sh` 和 `experiment.sh` 将两种角色分别配置：

| 角色 | 模型与配置 | 凭据 |
| --- | --- | --- |
| 轨迹采集、dev 验证、候选重跑 | 冻结执行配置；当前 `.env` 中选择 `deepseek-v4.1-flash` | `EXECUTION_MODEL` / `EXECUTION_API_KEY` / `EXECUTION_BASE_URL` |
| Trace2Skill 分析与演化、SkillOpt 反思与修改、SkillPatch 诊断与生成 | `configs/appworld/optimizer.toml` 读取 `.env` 中的 `METHOD_MODEL` | `METHOD_MODEL` / `METHOD_API_KEY` / `METHOD_BASE_URL` |

在选定的凭据文件中填写以下两个空位，URL 使用完整的兼容 API 根地址（包含服务要求的 `/v1` 等路径）：

```dotenv
METHOD_API_KEY=
METHOD_BASE_URL=
```

根目录 `.env` 已留出方法模型的 key 和 URL 空位；执行模型与方法模型均按角色配置，不绑定任何模型品牌。方法模型的配置映射可通过 `--optimizer-config <TOML>` 替换；不需要把 key 写进 TOML。真实的统一实验会在采集之前检查方法模型凭据和 URL，缺失时立即提示。`--prepare-only` 允许方法模型 key 和 URL 暂时未填，结果的 `optimizer.ready=false` 和 `missing_environment` 会明确标记；生成的 `.invalid` URL 仅是准备模式的占位，不会用于真实运行。

单独的 `collect_trajectories.sh`、`resume_trajectories.sh` 只使用执行模型，不需要方法模型凭据。方法模型变更不要求重新采集执行轨迹；更换执行模型则应采集新的轨迹。统一方法入口保存不含密钥的 `optimizer.json` 和执行配置快照，三种方法的优化连接保持一致，执行预算和共享轨迹继续由原清单固定。

SkillPatch 的 agent harness 也独立配置。根目录 `.env` 当前选择：

```dotenv
SKILLPATCH_HARNESS=codex
```

可选值为 `codex`、`openhands`，统一入口会让诊断和补丁生成使用同一种选择，模型、key 和 URL 仍读取 `METHOD_*`。切换 harness 不会根据模型名自动发生，也不会更改 Trace2Skill/SkillOpt 的原始方法流程或任务执行器。`optimizer.json`、诊断/生成配置快照和 agent `request.json` 均记录实际 harness。

Codex 通过 `codex exec` 运行：优先使用 `harness/codex/codex-rs/target/{release,debug}/codex` 中已编译的程序，否则使用 PATH 中已安装的 Codex CLI。它显式使用方法模型的自定义 provider、Responses API、环境变量凭据、`workspace-write` 工作区和 JSONL 事件日志，记录超时、取消及终局错误；不会回退到本机已登录的 Codex 账号凭据。方法模型服务必须支持 Responses API，包括流式工具调用。仅 Chat Completions 成功不能证明 Codex agent 可用。当前适配所需 CLI 版本应支持 `--json`、`--ephemeral`、`--ignore-user-config`、`--ignore-rules`。

agent 的提示词、材料、报告路径及下游 JSON/引用校验仍使用同一套流程。独立 Stage1/Stage2 配置也可以在 `[diagnosis]` 中设 `harness="codex"`，并使用服务要求的原始模型名；OpenHands 的 `openai/` 前缀只由统一入口为 OpenHands 添加。

训练轨迹采集完毕、填好方法模型空位后，可只运行三种方法：

```bash
env -u EXECUTION_MODEL -u EXECUTION_API_KEY -u EXECUTION_BASE_URL -u METHOD_MODEL -u METHOD_API_KEY -u METHOD_BASE_URL -u SKILLPATCH_HARNESS PYTHONUNBUFFERED=1 \
  bash scripts/appworld/experiment.sh \
  --trajectory-manifest runs/appworld/full-20261026-v41/shared/trajectory_manifest.json \
  --optimizer-config configs/appworld/optimizer.toml \
  --env-file .env \
  --output runs/appworld/ds41-gpt55-methods
```

独立的底层适配器 CLI 仍接受自己的 `--model`、`--api-key-env`、`--base-url-env` 参数；独立 SkillPatch Stage1/Stage2 命令仍使用显式的诊断/生成配置。上述角色拆分由两个统一入口负责。

## 三种方法的全量单轮实验（tmux）

`experiment.sh` 默认读取根目录 `.env`，先采集完整官方 train（本机 90 个任务），随后按 Trace2Skill → SkillOpt → SkillPatch 顺序运行。三个方法使用同一初始 Skill 和这一次采集的完整轨迹。正式实验只有 SkillOpt 使用完整官方 dev（本机 57 个任务）进行门控；Trace2Skill 冻结 deepen 后的 Skill，不跑 dev；SkillPatch 不跑 dev，处理所有诊断问题及生成的候选。

启动 tmux：

```bash
tmux new-session -s appworld-full -c /home/lihanlun123/skillpatch-rebuild
```

在会话中执行：

```bash
set -o pipefail
env -u EXECUTION_MODEL -u EXECUTION_API_KEY -u EXECUTION_BASE_URL -u METHOD_MODEL -u METHOD_API_KEY -u METHOD_BASE_URL PYTHONUNBUFFERED=1 \
  bash scripts/appworld/experiment.sh \
  --env-file .env \
  --output runs/appworld/full-20261025 \
  2>&1 | tee runs/appworld/full-20261025.console.log
```

`env -u` 确保角色模型配置从指定文件读取。输出目录必须全新；再次运行时更换目录名。按 `Ctrl+b` 后按 `d` 退出查看而保持运行，`tmux attach -t appworld-full` 返回。总日志记录阶段变化，采集详细日志在 `collection.log`，方法详细日志在 `<方法>/run.log`；`result.json` 随阶段更新。

正式执行配置为 `configs/appworld/execution.full.toml`，模型名和连接读取 `.env` 中的 `EXECUTION_*`；执行预算为 100 步、4096 输出 tokens、临时请求错误最多重试 2 次。已有轨迹继续绑定采集时的模型快照；更换模型后使用新目录重新采集。SkillOpt 用完整 train 批次进行一次训练步骤，逐条轨迹反思后合并；关闭 slow/meta 更新，保留原 gate。被拒候选的重跑仅作为诊断证据，最终选中版本及其来源任务评分另存为 `selected_source_evaluation`。无修改、无提升是正常实验结果，不能据此判作连通失败，也不能仅凭 `completed` 认定每条模型分析都成功。

准备检查不调用模型：

```bash
bash scripts/appworld/experiment.sh --prepare-only --output runs/appworld/full-preview
```

这是一轮共享轨迹的全量改进，包含候选在训练任务上的重跑；不包含最终 test 集评估或 SkillPatch Stage3 接受、整合。采集出现错误时保留已完成存档，不发布不完整清单；可以用下文专用入口在原目录续采。若采集已完成，可以用它的完整清单在新目录重试选定方法：

```bash
env -u EXECUTION_API_KEY bash scripts/appworld/experiment.sh \
  --trajectory-manifest runs/appworld/full-20261025/shared/trajectory_manifest.json \
  --methods skillpatch \
  --env-file .env \
  --output runs/appworld/full-20261025-skillpatch-retry
```

复用入口会拒绝仅包含小样本的清单。不要删除或修改共享目录及其中的冻结执行证据。

### 中断后在原目录续采训练轨迹

先在原 tmux 会话中执行，或用 `tmux attach -t appworld-full` 返回。以下命令只继续采集训练任务，完成后不会自动启动三种方法：

```bash
cd /home/lihanlun123/skillpatch-rebuild
set -o pipefail
env -u EXECUTION_API_KEY PYTHONUNBUFFERED=1 \
  bash scripts/appworld/resume_trajectories.sh \
  --shared-dir runs/appworld/full-20261025/shared \
  --env-file .env \
  2>&1 | tee -a runs/appworld/full-20261025.resume.console.log
```

续采使用原执行配置、任务顺序和初始 Skill，核验并保留已完成任务；没有完整官方评分的当前任务从初始环境重跑。新任务仍写入原 `shared/execution/tasks/`，独立尝试目录保留失败历史和新运行日志。原 `execution/config.json` 与输入快照不变，全部任务完成后在原 `shared/trajectory_manifest.json` 发布完整清单。再次中断可重新运行同一命令，已有有效任务不会重复执行；并发续采会被锁拒绝。

给上述命令添加 `--prepare-only` 可只读检查可续采数量，不调用模型或修改产物。续采进度查看 `shared/result.json` 和 `shared/execution/result.json`；外层三方法的 `result.json` 保留原批次失败记录，不代表这次单独采集的状态。

## 三种方法的全流程连通测试

先按下文采集一份小规模共享训练轨迹，然后运行：

```bash
bash scripts/appworld/smoke.sh \
  --trajectory-manifest runs/appworld/shared-smoke/trajectory_manifest.json \
  --env-file .env \
  --output runs/appworld/all-methods-smoke
```

此入口使用清单内全部训练轨迹，建议提供 1–3 个任务的清单。执行模型、地址、预算均继承已冻结的原生配置；三个方法的优化模型独立读取 `configs/appworld/optimizer.toml`（模型名读取 `METHOD_MODEL`）。它会复制初始 Skill、准备方法输入、逐一执行三个方法并保存各自日志与结果。`--prepare-only` 只检查配置，不调用模型或执行器。

| 方法 | 实际测试的链路 | 验证策略 |
| --- | --- | --- |
| Trace2Skill | 同一轨迹 → 原分析和演化 → 修改后的 Skill/参考文档 → 原训练任务再执行 | 初始和候选各执行官方 dev |
| SkillOpt | 首次训练复用同一轨迹 → 反思、修改 → 候选 dev 执行和 gate → 候选原训练任务再执行 | 保留原接受/拒绝规则，拒绝也算连通 |
| SkillPatch | 同一失败轨迹 → 诊断 → 生成补丁 → 交付补丁 → 原训练任务再执行 | 不跑 dev，最多每任务一条候选 |

Trace2Skill 和 SkillOpt 默认使用官方 dev 列表第一个任务，`--dev-limit N` 可调整；验证任务与训练任务独立，不使用 test 集。SkillOpt 只执行一个训练步骤，关闭可选 slow/meta 更新；其首轮复用不会影响候选验证或后续真实执行。候选重跑不回流本轮改进，也不以连通测试结果强制接受候选。

某方法没有生成修改后的候选，或没有实际完成改进后执行，会记 `incomplete`；模型、环境或格式错误记 `failed`。任务得 0 分但官方评分有效，仍可记 `completed`。一个方法失败后会继续测试其他方法，输入完整性破坏或取消则停止。只有三个方法全部完成时，`complete_three_method_coverage=true`。

结果位于根目录 `result.json` 和各方法的 `result.json`，详细过程在 `<方法>/run.log`、`optimization/`；Trace2Skill/SkillOpt 的原任务复跑在 `source_replay/`，SkillPatch 重跑在自身 optimization 下。单独重试某方法时使用新输出目录：

```bash
bash scripts/appworld/trace2skill/smoke.sh --trajectory-manifest <共享清单> --output <新目录>
bash scripts/appworld/skillopt/smoke.sh --trajectory-manifest <共享清单> --output <新目录>
bash scripts/appworld/smoke.sh --methods skillpatch --trajectory-manifest <共享清单> --output <新目录>
```

单方法重试仍引用同一冻结清单，不覆盖之前的失败记录。原有 `skillpatch/smoke.sh` 保留独立的诊断/生成配置参数；统一入口则自动对齐原生执行配置。此测试不代表全量正式实验，也不包含 SkillPatch Stage3 整合与最终测试集评估。

## 1. 准备检查

```bash
bash scripts/appworld/collect_trajectories.sh --limit 3 \
  --output runs/appworld/collect-preview --prepare-only
```

不启动 AppWorld 或调用模型。检查任务、Skill、配置并保存执行计划；准备模式不会生成可用于改进的冻结轨迹清单。

执行设置在 `configs/appworld/execution.toml`。默认 DeepSeek、100 步、每次请求 4096 输出 tokens、300 秒超时、单任务串行执行。正式模型名称与预算可在运行前调整，采集存档后原任务重跑继承这套设置。任务清单来自本机官方 train 的 90 个任务，顺序保存在 `configs/appworld/train_tasks.json`。`--limit 3` 取前三个任务，恰好是首个场景的三个变体；`--limit 0` 或省略表示全部配置任务。

使用兼容网关时，填写网关实际支持的模型名及完整 API 根地址（例如带 `/v1`）；执行模型名直接传给服务，OpenHands 配置中的兼容模型名需要 `openai/` 前缀。诊断和生成 TOML 的显式 `base_url` 也应指向所用服务，它们不会自动读取 `EXECUTION_BASE_URL`。采集时解析后的执行地址会存档，补丁重跑继承该地址。

原生执行可在 `[agent]` 下设置 `max_retries = 2` 来处理短暂连接错误和临时服务状态，默认 0。实际请求次数和重试次数会保存；不重试认证失败或无效、截断的模型输出。重试耗时包含在整体任务执行时间内。

`workspace/dataset/appworld` 是源码仓库；配置默认指向已下载数据和 AppWorld Python 环境所在的 `workspace/experiments/appworld-ds/`。如移动数据，修改 TOML 中的路径。初始 Skill 为 `configs/appworld/initial_skill.md`，复制自本机 EviSkill 的 `skills/appworld_initial.md`，三个方法应使用同一文本。

## 2. 采集共享轨迹

小规模采集：

```bash
bash scripts/appworld/collect_trajectories.sh --limit 3 \
  --output runs/appworld/shared-smoke
```

正式采集：

```bash
bash scripts/appworld/collect_trajectories.sh \
  --output runs/appworld/shared-train
```

采集成功后输出 `trajectory_manifest.json`。它记录原生执行存档、同一初始 Skill、执行配置、任务快照及每条轨迹的摘要。成功和失败轨迹均保留；执行异常或评分无效会导致采集失败，不发布可用于改进的清单。不要只复制单个任务 JSON：补丁重跑还需要整个原生执行目录及诊断、生成存档。

## 3. SkillPatch 流程测试

```bash
bash scripts/appworld/skillpatch/smoke.sh \
  --trajectory-manifest runs/appworld/shared-smoke/trajectory_manifest.json \
  --output runs/appworld/skillpatch-smoke
```

默认取清单前三条轨迹，每个任务最多处理一条诊断问题。`--task-limit N` 和 `--max-issues-per-task N` 可覆盖限制，`0` 表示全部。传入 `--prepare-only` 时只准备 Stage1 材料并检查绑定，不调用模型、不生成补丁、不重跑。

成功轨迹正常跳过；无改进建议、证据不足或无法生成补丁会明确记录。测试只有实际完成至少一次候选重跑，才记录为完整覆盖。全部成功或没有候选时返回 `status=incomplete` 和退出码 3，可扩大样本重试。任务重跑仍失败属于有效结果，不要求补丁一定提高分数。

如果补丁已生成而重跑阶段报错，修复原因后可用 `python -m set_up.cli validate-appworld-patch --patch <generation/generation/patch.json> --output <新的重跑目录> --env-file <密钥配置>` 单独重试该候选。保留原实验目录的失败记录，重试结果单独保存，不会回写为原脚本成功。

## 4. SkillPatch 正式单轮改进

```bash
bash scripts/appworld/skillpatch/experiment.sh \
  --trajectory-manifest runs/appworld/shared-train/trajectory_manifest.json \
  --output runs/appworld/skillpatch-experiment
```

默认处理清单全部任务和报告中全部问题，与流程测试调用同一套代码。诊断使用 `configs/skillpatch-stage1.toml`，生成使用 `configs/skillpatch-stage2.toml` 及其引用的生成配置，可通过 `--diagnosis-config`、`--stage2-config` 覆盖。运行开始会快照实际提示词及解析后的配置。它们的模型和显式 base_url 位于各自 TOML，与目标执行模型分别保存。

每个候选都依据冻结的原始轨迹生成，并独立对照其来源执行结果；候选之间不累计，重跑轨迹不回流本轮优化。一个任务产生多个候选时会分别重跑一次。

## 恢复中断的 Trace2Skill / SkillOpt 并运行最终测试

对于已完成 Trace2Skill evolution、但中断在 dev 评估阶段的实验，使用：

```bash
bash scripts/appworld/recover.sh \
  --source runs/appworld/full-20261026-v41/methods \
  --output runs/appworld/full-20261026-v41/recovery \
  --wait-for runs/appworld/full-20261026-v41/pipeline_status.json
```

该入口读取根目录 `.env` 的凭据，保持本轮冻结的模型和配置，不覆盖原实验。`--wait-for` 等待现有流水线退出后才使用 AppWorld 服务。Trace2Skill 复用已生成的 Skill，跳过 dev，冻结后直接进行最终测试。SkillOpt 原先尚未开始优化，恢复时重新启动 trainer，首次训练 rollout 仍复用冻结的 90 条轨迹，dev 门控保持原实现。验证阶段相同任务、相同 Skill、相同执行配置的已完成结果可共享。

优化完成后冻结最终版本，在官方 `test_normal`（168）分别评估 initial Skill、Trace2Skill Skill、SkillOpt 的 `best_skill.md`。测试不反馈给优化，也不做候选选择。不再将训练集回放作为最终成绩。默认只跑 `test_normal`；显式添加 `--test-splits test_normal test_challenge` 才会扩展至两个官方测试划分。

结果在 `recovery/result.json`、`recovery/trace2skill/validation.json`、`recovery/skillopt/result.json` 和 `recovery/test/<initial|trace2skill|skillopt>/`。同一命令再次执行会核验并复用已完成的执行；连接错误不算任务失败，异常尝试另存。若 SkillOpt trainer 中断，它会在新的优化尝试目录重新启动，已完成的相同执行仍可复用。此入口只处理 Trace2Skill 与 SkillOpt，不改变当前 SkillPatch 的生成或交付方式。

`--prepare-only` 只冻结和核验输入，不调用模型。续跑使用原实验冻结的模型，因此若要更换本轮方法模型，应另建实验，不混用已有分析和新模型结果。

## SkillPatch 最终 test_normal 评估

`set_up.appworld_patch_test` 接收已完成的 `appworld_patch_resume` 训练结果，冻结同一 initial Skill 以及全部生成的补丁。补丁按照固定训练顺序保存在 `SKILL_PATCHES.md` / `SKILL_PATCHES.json`；不按原任务是否修复筛选，也不根据测试结果选择补丁。通过现有 `run_native(..., patch_skill_dir=...)` 和 `PatchedSkillBank` 将 Skill 与补丁补充文本拼接交付执行模型，不改写 initial Skill。

```bash
.venvs/methods/bin/python -m set_up.appworld_patch_test \
  --training runs/appworld/full-20261026-v41/skillpatch-recovery \
  --experiment runs/appworld/full-20261026-v41/methods \
  --output runs/appworld/full-20261026-v41/recovery/test/skillpatch \
  --env-file .env
```

只运行官方 `test_normal` 的 168 个任务，执行模型与原冻结配置一致。结果及成功率保存在 `recovery/test/skillpatch/result.json`，每项存档核验 Skill 文本、补丁交付摘要和正式评分。可重复执行相同命令续跑，已完成任务不会重新请求模型。训练未完成时拒绝测试，连接异常不会计为任务评分为零。没有生成补丁时明确记录 `patch_count=0`，评估 initial Skill 本身。

本次 `restart_pipeline.py` 已在训练和另两种方法测试之后加入此阶段。最终四组比较为 initial、Trace2Skill、SkillOpt best、initial + SkillPatch patches；补丁测试结果不回流训练。

## 每五分钟后台监测

本次实验的监测入口为 `python -m set_up.appworld_watchdog --experiment runs/appworld/full-20261026-v41`，在 `appworld-monitor` tmux 会话中常驻运行。每 300 秒检查 initial、Trace2Skill、SkillOpt、SkillPatch 的进程、结果和进度，日志为 `monitor_history.jsonl`，最新快照为 `monitor_status.json`。对仍在运行的实验不重复启动；已完成的结果不重跑；退出的已知临时异常会使用原目录和缓存续跑。相同进度连续重启三次仍失败时标记 `needs_review`，保留诊断现场，避免盲目重启。余额/权限异常会检查接口是否已恢复，恢复后再续跑；未知代码或输入完整性异常需根据日志修复，监测程序本身不会擅自修改实验配置。

AppWorld 执行模型耗尽固定输出预算时，重试耗尽后记录 `termination_reason=model_output_limit`，结束该任务并调用 AppWorld 官方评分，而非手写零分。没有完整动作就不执行残缺代码；网络、鉴权及服务故障仍保持未评分的基础设施错误。既有完整执行结果可复用，输出预算、模型与任务不改变。

## 结果与范围

- `plan.json`：本轮选中的任务、预算、配置及策略。
- `trajectory_manifest.snapshot.json`：本轮输入清单快照，源存档继续通过摘要核验。
- `tasks/000001/diagnosis/`：Stage1 输入、报告及会话记录。
- `tasks/000001/issues/000001/generation/`：Stage2 生成结果与补丁。
- `tasks/000001/issues/000001/replay/`：部署文件、重跑存档及 `comparison.json`。
- `result.json`：阶段、任务和问题状态，候选数量、完成重跑数量及修复数量；错误时保留已完成产物并退出。

退出码：0 为完成或准备成功，2 为错误，3 为 smoke 未覆盖候选重跑，130 为取消。`repaired_count` 统计候选重跑次数，不是独立任务成功率。

这一版产出候选及原任务修复证据，`acceptance=not_decided`。补丁接受规则尚未确定，因此不进行自动接受、Stage3 整合或最终合并版本的测试集评估。`experiment.sh` 表示全量单轮改进入口，不代表已完成完整的论文评估。需要固定版本后再接统一测试入口；当前脚本不读取 dev/test 任务。

执行通道若要求流式 Chat Completions，可在根目录 `.env` 设置 `EXECUTION_STREAM=true`。原生执行器会汇总完整流式响应并检查终止标记，断流按连接错误处理，保持执行模型及采样参数不变。默认仍使用非流式；服务端明确返回非流式禁用的 400 时也会自动切换。

原生执行器对空正文或 `finish_reason=length` 的动作会按冻结配置中的 `max_retries` 重试相同请求；不提高 token 预算，不执行残缺代码，也不把执行异常视为任务得分为零。每个新执行目录的 `model_responses.jsonl` 保存结束原因、正文/推理长度和服务端返回的 token 用量，便于区分空回复与截断。格式错误、内容过滤等不自动重试。
