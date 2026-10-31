# SkillsBench 三方法实验

只新增编排脚本和配置，不修改 `method/`、方法适配器、执行器或 prompt。

默认任务源为 `workspace/dataset/skillsbench/tasks` 中的全部任务。
执行路径固定为 `Executor + SkillsBenchExecutionAdapter + BenchFlowBackend`，
不使用三个原生数据集的执行器。OpenHands 是 BenchFlow 内部的任务 agent；
SkillPatch 的方法 harness 独立读取 `.env` 的 `SKILLPATCH_HARNESS`。

实验顺序：

1. 使用每个任务附带的参考 Skill 包执行一次，保存完整轨迹、正式 verifier 反馈和文件哈希。
   收集阶段全部结束后才进入方法阶段。失败的环境运行单独记为失败，不伪装成 reward=0。
2. 每个方法只读取当前任务自己的参考轨迹。Trace2Skill 和 SkillOpt 对当前任务中的每个
   Skill 独立优化，始终从参考版本开始，不混入其他任务或已优化 Skill 的反馈。
   Trace2Skill 调用现有分析与演化入口；SkillOpt 调用原始 `ReflACTTrainer`，单轮单任务，
   原始 reflect、aggregate、select、apply、gate 均保留。
3. SkillOpt 的初始训练与初始验证复用同一参考轨迹，候选 gate 在原任务上真实执行。
   Trace2Skill 和 SkillOpt 完成各 Skill 的优化后，把这些 Skill 组装为完整包，重新执行原任务一次。
   SkillOpt 使用原训练器 gate 选出的 `best_skill.md`；未通过 gate 的候选及其分数仍在训练目录。
4. SkillPatch 调用原有诊断、生成和验证入口，对所有报告的问题逐个生成独立候选并在原任务验证。
   不额外限制问题数、不自行合并候选。保留原方法对已成功轨迹的跳过规则；没有候选时也保存
   一次原 Skill 的重跑分数，并标明没有修改。

验证集就是产生优化反馈的原任务，属于同任务优化评估，不能称为独立测试集上的泛化效果。
每种方法保留其他 Skill 和辅助文件；不更改原参考包。原任务 `task.md` 的 agent、verifier、
sandbox 预算保持原样；额外 idle 超时关闭。不添加执行总时限、步数或 token 限额。
方法自己的模型请求和编辑参数沿用现有默认；单轮配置只关闭跨轮 slow/meta update。

模型沿用根目录 `.env`：`EXECUTION_MODEL` / `EXECUTION_API_KEY` /
`EXECUTION_BASE_URL` 用于任务执行，`METHOD_MODEL` / `METHOD_API_KEY` /
`METHOD_BASE_URL` 用于三种优化方法。凭据不会写入实验 plan。

```bash
# 只准备一个任务，核验快照及 prompt，不调用模型、不启动容器。
.venvs/methods/bin/python -m scripts.skillsbench.experiment \
  --prepare-only --tasks citation-check --output runs/skillsbench/preview

# 全量，预算全部来自各自任务。
.venvs/methods/bin/python -m scripts.skillsbench.experiment \
  --output runs/skillsbench/full

# 使用相同配置恢复；保留已完成项，不自动重试失败项。
.venvs/methods/bin/python -m scripts.skillsbench.experiment \
  --output runs/skillsbench/full --resume
```

`--tasks` 显式选择任务，`--methods` 显式选择方法；默认全部任务和三种方法。
`plan.json` 保存实验设定、任务和实现哈希；`trajectory_manifest.json` 绑定共享轨迹；
`result.json` 保存进度和分数，方法细节位于 `<method>/<task>/run.log` 和各阶段产物。
恢复时核验配置、源任务和实现哈希。进程意外终止在尚未保存的阶段时，需要先检查已有阶段产物，
脚本不会覆盖已有目录或悄悄重复收费调用。

安装网络：`bench.py` 使用原安装环境的 Python 调用已安装的 BenchFlow，
只给原始 OpenHands 安装命令添加容器可访问的代理。Git commit、SDK/tools 版本、
agent 启动命令和任务执行预算均保留；OpenHands 安装超时单独设为 1800 秒。代理不加入后续 agent 启动命令。
单个任务异常保存后继续整批；缺少有效轨迹时不进入该任务的方法阶段。

安装网络补充：安装阶段的 uv 脚本改用 Astral 官方 GitHub Releases 地址，Python 包索引使用清华 PyPI 镜像。仅作用于 installer 子进程；OpenHands 固定 commit、SDK/tools 版本、方法、模型与任务预算不变。两项分别由 bench.py 的 --uv-installer-github 和 --install-index 控制。

自定义执行模型网关：bench.py 将执行进程中的 BENCHFLOW_PROVIDER_BASE_URL/API_KEY 作为 OpenHands 显式 provider 配置传给原 resolver，防止继承配置被默认 OpenAI provider 覆盖。API key 仅由环境传递，不写入 bench 配置。方法模型的配置不受影响。

--ubuntu-install-mirror 仅为 installer 创建临时 APT 配置，使用清华 Ubuntu 镜像；保留原发行版、组件和 Ubuntu 归档密钥验证，不修改任务的系统源文件或 agent/verifier 预算。

安装超时由 bench.py --install-timeout 1800 设置，仅覆盖 OpenHands registry 的 install_timeout；任务 task.md 的 agent/verifier/sandbox 预算不变。

下载连接超时：installer 子进程设置 UV_HTTP_TIMEOUT=300，避免慢速依赖下载被默认 30 秒单次请求超时中断；总安装时限仍为 1800 秒。

并发和失败处理：--concurrency 4 使用最多四个独立工作进程；各阶段分别保持上限，仍先收集完整基线批次，再运行各方法。单个任务异常保存失败记录，不终止整批；缺少有效基线的三种方法记录 blocked，不伪装为 reward=0。全部尝试结束但存在失败或 blocked 时，状态为 completed_with_errors。

--reuse-baselines-from /absolute/previous-output 可复用已结束实验中 completed 的基线；复用前核验执行配置、模型、实现、任务与归档哈希。失败和未开始任务重新执行；新实验输出目录必须独立。


## 仅执行基线及人工复核

`--baseline-only --concurrency 4` 完成基线后停止，不启动方法。
`scripts.skillsbench.baseline_review --config <workflow-config.json>` 可等待既有基线结束、仅重跑一次执行超时任务，并复制合并两轮归档。配置字段见该模块，运行目录需使用独立路径。
执行超时计 0 分；安装、模型和其他基础设施异常单独统计。合并优先选择有效的重跑结果，不按最高分挑选；两轮原始结果均保留。完成后等待人工决定后续方法阶段。

容器代理启动方式见 [代理转发说明](../proxy/README.md)。配置中的绝对路径、模型及网关需按运行机器调整。
