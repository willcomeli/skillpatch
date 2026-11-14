# ScienceWorld：空白 Skill → SkillOpt → SkillPatch

实验编排保留 `method/` 和方法提示词；执行器仅增加关闭显式输出 token 上限的支持。默认执行模型为
`deepseek-flash`，方法模型为 `gpt-6.1-sol`；网关 model ID 可在 `.env.scienceworld` 修改。
SkillOpt 方法请求也省略 `max_tokens` / `max_completion_tokens`，使用 API 默认输出上限。
同一任务首次失败后默认最多重试 5 次（最多 6 次失败尝试），耗尽后记录 `skipped.json` 并继续下一个任务。
设置 `SCIENCEWORLD_TASK_MAX_RETRIES=unlimited` 后，连接等基础设施故障会无限重试当前任务，
不会因为重试次数耗尽而跳过；仍可随时暂停并从检查点恢复。
执行模型请求内重试由 `SCIENCEWORLD_REQUEST_MAX_RETRIES` 设置，默认 `0`；设为 `unlimited`
时，在原环境、原步骤中无限重发相同请求，保留已执行动作和对话历史。
跳过任务单独列出，从评分与成功率分母中排除；实际评分数量可能小于固定划分数量。

## 实验协议

1. 从固定 SkillNet 提交 `edfbda23ac2108b59579300d050218558d20f6a6` 下载
   `train_indices.json`、`valid_indices.json`、`test_indices.json`。
   按 EviSkill `build_scienceworld_split.py` 的零基索引选取训练 96 条、验证 48 条，
   保留测试全部 211 条。检查训练每类 4 条、验证每类 2 条，共 24 类，并检查三个划分的
   `(task, variation_idx)` 不重合。源文件及任务身份摘要保存到 `inputs/splits/`。
2. 按实验所有者确认，使用 EviSkill 代码默认 `simplification=""`。
   上游未公开 EviSkill 转换后的三份清单，无法从公开材料证明其正式实验的该字段；
   本脚本记录为依据 SkillNet 和 EviSkill 索引重建任务身份，不声称匹配未公开文件的字节指纹。
   不使用 SkillNet 的 `max_steps.json` 或自行添加 `recommended_max_steps`。
3. 空白文件作为 SkillOpt 初始 Skill，直接调用固定版本的原始 `ReflACTTrainer`，
   复用已有 `SharedExecutionAdapter` 的反思、输入转换与划分接口。
   接入上游 `SplitDataLoader`：每个 epoch 确定性打乱全部 96 条，再按 40、40、16 分批，
   每轮不重复且完整覆盖训练集；恢复时复现相同批次。保留默认 4 epochs、slow/meta update、
   验证门控及最终测试。adapter 将批次原样交给执行器，再转换轨迹供方法反思。
   恢复及模型重试仅包装 I/O，不重写优化方法。
4. 取 SkillOpt 的 `best_skill.md`，在训练集的 96 个任务上分别收集新轨迹。
   运行一轮已有 Stage1/Stage2。保留原方法对成功任务的跳过规则；
   对报告的每一个问题生成候选，不限制问题或补丁数量。
5. 接收所有成功生成且通过已有结构、来源和目标绑定校验的补丁，按训练任务及问题顺序
   写入一份 `SKILL_PATCHES.json` / `SKILL_PATCHES.md`，不做验证筛选、语义合并或 Stage3 决策。
   原 SkillOpt Skill 正文保留。`unpatchable` 和 `insufficient_evidence` 是正常无补丁结果；
   模型协议、结构或绑定错误会停止，不被静默跳过。
6. SkillOpt 分数复用其原训练器最终 `best_skill` 在完整测试集上的执行存档，避免重复收费执行。
   最后用同一测试集运行带全部补丁的 Skill，报告平均归一化得分和成功率。
   SkillPatch 不读取验证集或测试集来生成补丁。SkillOpt 自身默认的空白/末轮 Skill
   测试仍保留，最终对比使用 `best_skill` 测试，而非按测试分数挑选 Skill。

ScienceWorld 沿用已下载环境及现有执行器默认 100 步。没有实验、任务或训练总时长限制，
执行配置使用 `agent.max_tokens = false`，请求省略 `max_tokens`，由 API 服务决定输出上限。
没有另加步数限制。现有执行器的单次 API 请求超时默认 300 秒，
方法 API 单次请求为现有适配器的 1800 秒；SkillPatch 诊断/生成 Agent 整个会话按用户要求
设为 1800 秒。会话超时保留产物并重试该阶段。

## 本机依赖

需要 Java（先确保 `java -version` 成功）、uv、Python 3.12、SkillOpt 子模块和可用的 Codex CLI。
ScienceWorld 默认读取仓库旁边已下载的 `../datasets/ScienceWorld`，可用 env 覆盖路径。

```bash
cd /Users/willcomeli/work/skillpatch/skillpatch
git submodule update --init method/skillopt
uv venv .venvs/methods --python 3.12
uv pip install --python .venvs/methods/bin/python -r method_input_adapters/requirements.lock.txt
uv pip install --python .venvs/methods/bin/python --no-deps -e ./method/skillopt
uv pip install --python .venvs/methods/bin/python -e ../datasets/ScienceWorld

# 确认当前机器可用的 Codex CLI。
codex --version
```

SkillPatch 诊断和补丁生成默认使用 Codex，通过 `METHOD_MODEL` 选择 GPT 方法模型。
可用 `SKILLPATCH_AGENT_COMMAND` 指定 Codex 可执行文件；不指定时，项目接口优先使用
本地编译的 Codex，再查找 PATH 中的 `codex`。当前本机已有 Codex CLI。
同一 `METHOD_BASE_URL` 需要同时支持 Codex 的 Responses 和 SkillOpt 的 Chat Completions。
本脚本没有自动修改系统 Java 或安装模型服务。

## 配置和运行

已提供根目录 `.env.scienceworld`（Git 忽略），可分发的模板位于
`configs/scienceworld/experiment.env.example`。填入两组 `BASE_URL` 和 `API_KEY`，
URL 使用 API 根地址，不附加 `/chat/completions`。如网关 ID 不同，同时修改模型字段。
env 文件不执行 shell 展开；已导出的同名非空环境变量优先于文件。

```bash
# 填好 URL 后先准备：不请求模型、不启动 Java 环境。
bash scripts/scienceworld/run.sh --env-file .env.scienceworld \
  --output runs/scienceworld/main --prepare-only

# 启动与自动恢复；保留上一步准备的配置。
bash scripts/scienceworld/run.sh --env-file .env.scienceworld \
  --output runs/scienceworld/main --resume
```

也可直接运行第二条命令并省略 `--resume`，在全新输出目录中准备后立即执行。
换 URL、模型或代码后使用新的输出目录；准备后只补填密钥不影响恢复。
旧 `runs/scienceworld/main` 使用独立采样批次，属于旧协议；保留其产物，不用修正后的代码
续跑或混合结果。修正后的完整遍历实验需使用新输出目录（例如 `runs/scienceworld/full-epoch`）。
无需提前手动创建输出目录。终止后用同一个 `--output ... --resume` 继续。

## 并发调度

在 `.env.scienceworld` 中配置（默认都为 `1`，保留串行）：

```dotenv
SCIENCEWORLD_EXECUTION_WORKERS=10
SCIENCEWORLD_SKILLPATCH_WORKERS=10
SCIENCEWORLD_TASK_MAX_RETRIES=unlimited
SCIENCEWORLD_REQUEST_MAX_RETRIES=unlimited
```

第一项控制 SkillOpt 的训练、验证、测试，以及最终 SkillPatch 测试的任务进程数。
第二项控制 SkillPatch 阶段的训练任务进程数：每个进程收集该任务轨迹，再执行已有诊断
及问题生成流程；同一任务内先诊断、再按问题顺序生成。两种阶段不重叠，进程数不相乘。
这两个设置不改变 SkillOpt 原有的反思/合并线程数。

每个任务使用独立进程、ScienceWorld 环境、租用端口和输出目录。批内任务可并发，
全部完成或记录跳过后，主进程按任务清单顺序转换/汇总结果；SkillOpt 更新步骤仍串行，
每轮仍为 40、40、16。各任务按 `SCIENCEWORLD_TASK_MAX_RETRIES` 设置独立重试并支持断点恢复。
设置为 `unlimited` 时，历史上因重试耗尽跳过的执行检查点也可重新执行，旧跳过记录和失败尝试保留。
历史缺失数据导致的训练分支仍需按依赖回滚重做，不能仅改变重试配置就复用已受影响的方法结果。
Ctrl-C 会停止所有任务进程组（包含环境和 harness），完成的检查点保留。
并发配置、任务重试和请求重试配置写入 `plan.json`，恢复时检查一致性；改变配置应使用新输出目录，
或在用户明确授权后记录恢复修订。
子进程请求及响应记录在各任务的 `.dispatch/` 下，密钥只通过进程环境传递。

## 恢复与产物

- 执行模型遇到连接中断、请求超时、429 或服务端暂时错误时，先按
  `SCIENCEWORLD_REQUEST_MAX_RETRIES` 原地重发相同请求。设为 `unlimited` 时不因网络故障
  退出当前任务，不重置环境、不重复已经执行的动作，也不消耗额外任务步数。
  重试等待沿用现有的 1、2、4 秒退避，之后每次等待 4 秒；支持 Ctrl-C 停止。
  未返回完整动作时也在模型客户端内重试，不将其记为任务 0 分或改变输出预算。
  请求内重试预算耗尽（有限配置时）、环境连接失效或进程中断后，才由外层恢复任务；
  原生环境不支持从中间动作持久化 Java 状态，因此这种恢复需要从当前任务开头重跑。
  外层任务重试由 `SCIENCEWORLD_TASK_MAX_RETRIES` 控制，默认 5，设为 `unlimited` 后无次数上限。
  方法模型请求与诊断/生成阶段仍沿用原有恢复逻辑。
- SkillOpt 使用上游的训练检查点。任务级执行与完整 rollout 另有精确 Skill/任务绑定的检查点，
  已完成项校验后复用。未完成优化步可能重新反思/生成；不承诺逐 token 恢复。
- SkillPatch 对诊断与每个问题分别记录完成状态。失败尝试保留，成功阶段不重复运行。
- 外层监督进程在工作进程异常退出后自动重启；`Ctrl-C` 明确停止。监督进程自身终止后
  需要重新运行 `--resume`，脚本没有安装系统后台服务。
- 401、缺少依赖、非法结果、来源改变等错误停止，绝不当作 reward=0，也不无限重试。
  输出目录加独占锁，避免两个监督进程同时运行一个实验。

`plan.json` 固定输入及实现摘要；`experiment.log` 为凭据值脱敏后的总日志；
`skillopt/trainer/` 为上游完整训练产物；`skillpatch/` 为训练轨迹及逐问题诊断/生成存档；
`final_skill/` 为原 Skill 和全部补丁；`patches.json` 记录来源；
`test/{skillopt,skillpatch}/summary.json` 和根 `result.json` 给出最终比较。
测试汇总同时报告原始数量、实际评分数量、跳过数量和跳过 ID；得分只统计实际完成的任务。

```bash
python3 -m unittest tests.test_scienceworld_experiment -v
```
