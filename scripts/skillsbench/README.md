# SkillsBench 三方法实验

本机本轮实验仅运行 SkillOpt 和 Trace2Skill，暂不运行 SkillPatch：

```bash
.venvs/methods/bin/python -u -m scripts.skillsbench.experiment \
  --config configs/skillsbench/local.toml --env-file .env.scienceworld \
  --methods skillopt trace2skill --concurrency 10 \
  --reuse-baselines-from runs/skillsbench/full-4workers-20261114 \
  --reuse-after-step-retry-upgrade \
  --output runs/skillsbench/full-10workers-step-retry-20261114
```

使用 v1.1 的全部 87 个任务，执行模型为 `deepseek-flash`，方法模型为
`gpt-6.1-sol`。同一任务轨迹分别用于包内各 Skill 的优化，组包后重跑原任务。
方法编排使用已存在的 methods Python；BenchFlow 后端由 `local.toml` 指定的
本机 Python 调用已安装的 BenchFlow 0.6.3，不使用 `benchflow_source`。
Docker Desktop 的镜像拉取代理与容器安装代理分别配置；修复前的失败归档保留在旧目录。

`local.toml` 的 `--step-retries` 开启原地网络重试，次数无上限：

- 构建阶段重发同一个 Docker build，复用成功的镜像层，重新执行失败的 RUN 层。
- 安装阶段保留同一容器和缓存，重试安装步骤。`--no-install-timeout` 或运行目录中的
  `INSTALL_POLICY_REQUESTED.json`（`total_timeout_seconds: null`）取消整个安装命令的
  总时限，Docker exec 同样不设总时限；flock 和成功标记防止丢失响应后重复启动安装。
  单次网络请求的超时与原地重试保留。
- OpenHands 在原 SDK 的单次 LLM 请求边界内重试网络错误；流式响应缺少结束标记时
  也重发同一请求，不重新执行已完成的工具动作。SDK/CLI 版本、模型参数和 prompt 不变。
- SkillOpt/Trace2Skill 的方法模型在 OpenAI SDK HTTP 请求边界内重试，保留方法上下文。

任务自身的 Agent/verifier 总时限不重置；认证、证书验证、缺失软件包、平台不匹配等
非瞬时网络错误不会无限重试。构建与安装的失败尝试日志保存在各轨迹目录。
运行目录的 `APT_MIRROR_REQUESTED.json` 可通过 `build_proxy` 指定已有的容器可达代理。
构建只在当前 RUN 中设置临时 `APT_CONFIG`，保留任务显式的 APT 配置；镜像运行时不继承
该变量。发行版、软件包版本、APT 签名和哈希检查保持原样，代理及变更哈希写入路由记录。
额外批准的准备修正可登记在 `ADDITIONAL_PREPARATION_RECOVERY_REQUESTED.json`；
它们在整批准备补跑结束后、任何方法开始前执行。仅接受失败归档哈希匹配、无 Agent
轨迹且无正式评分的源任务；已经完成的结果可恢复，部分执行结果不自动从头重跑。
`PREPARATION_COMPATIBILITY_REQUESTED.json` 登记逐项批准的兼容依赖及插入位置；只修改
SDK 临时 Dockerfile，保留原始数据集文件，安装使用精确版本和 `--no-deps`，记录修改前后
哈希。当前地震相位关联与地震相位拾取任务补充 `setuptools==81.0.0`，其余固定依赖保持原样。
新实验复用 12 项已核验的正式评分基线，其余 75 项补跑。复用前检查原任务快照、执行模型、
执行器、prompt、完整执行请求及归档哈希；仅允许新增 `--step-retries` 这一传输设置差异。
新实验的 `baseline_reuse_audit.json` 保留旧 plan 和来源记录，旧归档不改写。

本轮随后经用户确认启用 APT 镜像。运行目录的 `APT_MIRROR_REQUESTED.json` 是该实验的
显式路由设置，`bench.py` 会沿原生 `--config` 路径识别它；独立新运行也可传 `--apt-mirror`。
只改 BenchFlow 的临时 Dockerfile 和准备中的容器软件源，不改数据集或归档中的任务快照。
Ubuntu、Ubuntu-ports、Debian、Debian-security 使用中科大镜像，保留原 HTTP/HTTPS 协议、
发行版代号、组件、签名密钥及验证；不添加 trusted/allow-unauthenticated 等选项。
新源可能使未固定版本的系统包发生差异，也会使相关构建缓存层失效。

各运行的 `mirror-routing/` 记录 Dockerfile 改写和 Agent 安装前后的包版本；
本轮既有准备上下文的补充记录在 `mirror-live/`。已进入 Agent 阶段的容器不改源，
只读取包清单。复用的 12 个旧归档保留原环境；未保存的旧包版本不能追补为实测数据。
`transport_policy_revisions.jsonl` 和更新前的 plan 保存网络恢复修正与镜像启用的代码哈希。
`RUNTIME_PYTHON_INDEX_REQUESTED.json` 可单独指定新后端的 OpenHands 安装和 verifier
包下载索引，并在后端保存路由记录。未登记 `build_tasks` 时不重写构建命令；
已启动的安装和构建保持原进程设置。本轮后续后端采用已测通的中科大 Python 镜像，
固定版本的 pytest 包哈希、CasADi 索引哈希与官方一致，包安装仍执行原完整性校验。
用户另批准相位关联、相位拾取、LaTeX 三项在准备阶段切换同一 Python 镜像并重启，
这三项登记在 `build_tasks` 中，固定依赖和原生时限保持原样。旧准备归档保留，
`preparation_batch_followup.py` 并发补跑，且拒绝覆盖已有 Agent 轨迹或正式评分的任务。
批次在 Energy 结果收集后统一收集，再进入原有两方法编排；无额外验证或测试阶段。
Energy 的固定依赖预缓存记录使用 `verifier-precache/preparation.json`，避免被执行器的
原生 `result.json` 收集逻辑误识别。可登记经官方 SHA-256 校验的原版 uv 发布归档，
通过原安装器支持的 `UV_DOWNLOAD_URL=file://` 使用本地缓存，原评分脚本仍照常安装。
当 Agent 已结束而评分下载失败，`verifier_recovery.py` 可用绑定原轨迹的最终报告仅恢复
native verifier；原始轨迹逐字复用、额外模型调用为 0，并单独记录恢复来源与报告哈希。
评分继续使用原生 240 秒时限和既有执行适配器收集反馈；旧失败归档保留。
`image_cache.py` 可为两个原始 BugSwarm 镜像做下载缓存：中断后按原始 blob 的字节
偏移续传，完整 SHA-256 校验后通过 OCI 载入本机 Docker，再核对根文件系统 diff IDs
和架构。它不改镜像内容、任务并发或 Agent；载入成功才删除工具自己的重复下载文件。
HuggingFace 模型预下载使用同一固定 commit，通过已有代理和官方 HTTP 后端下载；
瞬时网络错误在同一 RUN 内重试 `snapshot_download`，保留已下载文件缓存，证书和
认证错误不重试。此注入仅在登记的模型准备 RUN 中生效，不进入 Agent 运行环境。

原生 Agent 执行超时且正式 verifier 已正常评分的任务，按 verifier 的原分数统计并继续
SkillOpt/Trace2Skill。超时不强制等于 0 分；原始执行归档保留 `status=timeout`。
安装、连接、verifier 等基础设施异常不会因此被转为有效评分。
本轮运行中的策略更新在基线收集结束后交接，保留更新前的 plan/result、代码哈希和恢复清单，
不重启正在执行的任务。交接记录为运行目录内的 `scored-timeout-policy-handoff.json`。

重试集中在编排脚本，不修改 `method/`、执行器源码或 prompt。
共享归档读取器支持使用已有正式评分的 Agent 超时轨迹。

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
方法自己的模型请求参数和编辑参数沿用现有默认；网络重试策略见上述本机设置。
单轮配置只关闭跨轮 slow/meta update。

模型沿用根目录 `.env`：`EXECUTION_MODEL` / `EXECUTION_API_KEY` /
`EXECUTION_BASE_URL` 用于任务执行，`METHOD_MODEL` / `METHOD_API_KEY` /
`METHOD_BASE_URL` 用于三种优化方法。凭据不会写入实验 plan。

```bash
# 只准备一个任务，核验快照及 prompt，不调用模型、不启动容器。
.venvs/methods/bin/python -m set_up.cli skillsbench experiment \
  --prepare-only --tasks citation-check --output runs/skillsbench/preview

# 全量，预算全部来自各自任务。
.venvs/methods/bin/python -m set_up.cli skillsbench experiment \
  --output runs/skillsbench/full

# 使用相同配置恢复；保留已完成项，不自动重试失败项。
.venvs/methods/bin/python -m set_up.cli skillsbench experiment \
  --output runs/skillsbench/full --resume
```

`--tasks` 显式选择任务，`--methods` 显式选择方法；默认全部任务和三种方法。
`plan.json` 保存实验设定、任务和实现哈希；`trajectory_manifest.json` 绑定共享轨迹；
`result.json` 保存进度和分数，方法细节位于 `<method>/<task>/run.log` 和各阶段产物。
恢复时核验配置、源任务和实现哈希。进程意外终止在尚未保存的阶段时，需要先检查已有阶段产物，
脚本不会覆盖已有目录或悄悄重复收费调用。

Docker 清理后若遗留尚未收集的 SkillOpt 任务目录，可先运行：

```bash
.venvs/methods/bin/python -u -m scripts.skillsbench.resume_methods \
  --output runs/skillsbench/full-10workers-step-retry-20261114
```

该恢复入口先调用原实验的 resume 核验，不改原 plan 或方法配置。已写出最终 summary、
checkpoint 和 best_skill 的单个 Skill 直接复用；未完成 Skill 在新目录中恢复优化，旧产物不覆盖。
由于原容器已删除，未完成的执行需要新建环境。最多 10 项并发，收集这批中断任务后自动交回
原实验，继续剩余 SkillOpt 和后续 Trace2Skill；已有 completed/failed/blocked 任务记录保留。

安装网络：`bench.py` 使用原安装环境的 Python 调用已安装的 BenchFlow，
只给原始 OpenHands 安装命令添加容器可访问的代理。Git commit、SDK/tools 版本、
agent 启动命令和任务执行预算均保留；安装总时限可独立关闭。代理不加入后续 agent 启动命令。
本机使用 `host.docker.internal` 安装代理时，wrapper 同时为主机 Buildx 认证请求设置对应的
`127.0.0.1` HTTP(S) 代理；已显式设置的主机代理不覆盖。此设置仅在 BenchFlow 子进程中生效。
单个任务异常保存后继续整批；缺少有效轨迹时不进入该任务的方法阶段。

安装网络补充：安装阶段的 uv 脚本改用 Astral 官方 GitHub Releases 地址，Python 包索引使用可达的 PyPI 官方源。仅作用于 installer 子进程；OpenHands 固定 commit、SDK/tools 版本、方法、模型与任务预算不变。两项分别由 bench.py 的 --uv-installer-github 和 --install-index 控制。

自定义执行模型网关：bench.py 将执行进程中的 BENCHFLOW_PROVIDER_BASE_URL/API_KEY 作为 OpenHands 显式 provider 配置传给原 resolver，防止继承配置被默认 OpenAI provider 覆盖。API key 仅由环境传递，不写入 bench 配置。方法模型的配置不受影响。

--ubuntu-install-mirror 仅为 installer 创建临时 APT 配置，使用中科大 Ubuntu 镜像；保留原发行版、组件和 Ubuntu 归档密钥验证，不修改任务的系统源文件或 agent/verifier 预算。

`bench.py --no-install-timeout` 或上述运行目录标记将 OpenHands registry 的
`install_timeout` 设为 `None`，优先于旧的 `--install-timeout 1800` 参数。
BenchFlow 原生配置记录与实际 Docker exec 都使用 `None`；不会改变任务的 Agent/verifier 预算。
新策略在新启动的后端中生效，已经运行的 Python 后端不会自动重载。

下载连接超时：installer 子进程继续设置 UV_HTTP_TIMEOUT=300；关闭安装总时限不关闭请求超时及重试。

并发和失败处理：--concurrency 4 使用最多四个独立工作进程；各阶段分别保持上限，仍先收集完整基线批次，再运行各方法。单个任务异常保存失败记录，不终止整批；缺少有效基线的三种方法记录 blocked，不伪装为 reward=0。全部尝试结束但存在失败或 blocked 时，状态为 completed_with_errors。

--reuse-baselines-from /absolute/previous-output 可复用已结束实验中 completed 的基线；复用前核验执行配置、模型、实现、任务与归档哈希。失败和未开始任务重新执行；新实验输出目录必须独立。


## 仅执行基线及人工复核

`--baseline-only --concurrency 4` 完成基线后停止，不启动方法。
`scripts.skillsbench.baseline_review --config <workflow-config.json>` 可等待既有基线结束、仅重跑一次执行超时任务，并复制合并两轮归档。配置字段见该模块，运行目录需使用独立路径。
执行超时计 0 分；安装、模型和其他基础设施异常单独统计。合并优先选择有效的重跑结果，不按最高分挑选；两轮原始结果均保留。完成后等待人工决定后续方法阶段。

容器代理启动方式见 [代理转发说明](../proxy/README.md)。配置中的绝对路径、模型及网关需按运行机器调整。
# Python download routing

APT transport failures in Docker build RUN instructions retry the same native
`apt-get` invocation, retaining downloaded archives inside that RUN. Index
warnings caused by network loss also retry when apt exits zero. Native package
selection, signature verification and cleanup hooks remain in place. The helper
PATH applies only to build RUN shells and does not change runtime Agent tools.

The run's `STARTUP_POLICY_REQUESTED.json` records approved startup changes.
`multilingual-video-dubbing` uses `DOCKER_DEFAULT_PLATFORM=linux/amd64` only
inside its own backend process, retaining all dependency pins. ACP startup
and prompt execution share the task's native Agent budget. The fixed 60s
initialize/session-new waits are removed using a private function namespace;
global asyncio and installed BenchFlow sources are unchanged. Both ordinary
and scene-role connection paths are covered. Existing processes retain their
loaded implementation; approved pre-prompt failures enter preparation recovery.

The approved mirror policy also sets the default pip/uv index to
`https://pypi.org/simple` in temporary build RUN commands,
OpenHands installation, and the native verifier environment. System pip/uv
configuration is created only when absent; explicit task indexes retain
precedence. Package pins, hash requirements, TLS verification and native
Agent/verifier deadlines are unchanged.

`mirror-routing/*.python-packages.json` records distribution names, versions
and metadata locations after installation and verification. `mirror-live/`
contains observations for processes already running when routing was enabled.
UV cache entries are explicitly labeled as cached; their presence does not
prove a verifier used them. Earlier completed archives are preserved, so their
version evidence is limited to the original logs.

2026-11-15 下载源切换记录：清华镜像对当前网段返回 403 拒绝页（包括 APT 和 PyPI），
后续新后端改用中科大 APT 镜像及 PyPI 官方源。已用原生 Ubuntu 容器完成
签名索引更新和 hello 包下载，并在 Python 3.12 ARM 容器完整下载原失败的
matplotlib 3.11.1 wheel。原固定版本、签名、TLS、哈希校验与原生任务时限不变；
既有有效基线继续复用，未启动 Agent 的受影响准备阶段追加补跑，原失败记录保留。

本轮用户批准的九项补跑由 `recover_references.py` 编排，读取运行目录的
`NINE_REFERENCE_RECOVERY_REQUESTED.json`，使用现有 Executor、适配器和
BenchFlowBackend。只向原协调进程发送 SIGINT，让其进程池完成当前工作后退出；
不向仍在运行的 Agent 或容器发送停止信号。空余并发名额用于新建九项补跑，
两批参考轨迹全部归档后，才恢复 SkillOpt → Trace2Skill。
`approved_nine_recovery_progress.json` 保存独立进度，原失败轨迹和记录保留；
已完成的补跑归档在整合时校验哈希。已有补跑输出时脚本拒绝直接重复启动，
以免重复执行 Agent；异常恢复应先核对存活进程与归档。

Docker 准备阶段仅对明确的 containerd ingest 提交时 `rename … no such file`
错误增加原地步骤重试。它不适用于模型或任务工具调用，也不重试权限、磁盘空间、
认证或证书错误；镜像标签、校验和任务原生预算不变。

九项补跑的并发调度已由 `recover_references_global.py` 接管，读取
`GLOBAL_REFERENCE_RECOVERY_REQUESTED.json`。它将仍在运行的旧任务和补跑任务
放入同一个并发计数，每次释放名额便提交等待项，合计最多 10 项；不再为补跑
固定保留 4 个 worker。旧协调进程仅停止提交，已有执行继续完成。
`global_reference_recovery_progress.json` 记录全局活动项、队列和归档，
两种优化方法仍在全部参考任务收集完成后启动。

Suricata 的 AlmaLinux/EPEL 下载故障通过 `RPM_MIRROR_REQUESTED.json` 按任务启用：
AlmaLinux 使用华为镜像，EPEL 使用中科大镜像，保留原发行版变量、架构、启用的仓库、
签名密钥与校验设置。临时构建 RUN 中的 DNF 包装器保留原参数和下载缓存，
仅对网络错误无限重试；原生请求超时不变，运行期 Agent 的 PATH 不修改。
RPM 实际版本清单与其他包版本一起保存到 `mirror-routing/`。

`preparation_followup.py` 为已确认“尚未启动 Agent”的失败准备阶段使用空闲名额，
与全局活动任务合计不超过 10 项。它通过运行目录的 `BASELINE_ONLY_REQUESTED.json`
设置自动收集屏障，等待所有原调度器退出后合并归档、移走屏障并自动恢复两种方法；
这个屏障不代表还需用户批准。原失败归档保留在原路径。

用户已批准能源任务预缓存评分依赖。`VERIFIER_PRECACHE_REQUESTED.json` 仅对
`energy-ac-optimal-power-flow` 生效：完成原生评分加固后，准备原版本 uv 0.9.7、
pytest 8.4.1、pytest-json-ctrf 0.3.5、numpy 1.26.4 和 casadi 3.6.7，
只运行 `pytest --version`，然后交回原评分函数启动原生 240 秒测试计时。
测试脚本、测试内容、签名与包校验不变；依赖准备移到计时之外是本轮明确记录的流程差异。
每个准备步骤单独网络重试，日志和准备耗时保存在 `verifier-precache/`。

该项通过 `preparation_followup.py --request-name ENERGY_PRECACHE_FOLLOWUP_REQUESTED.json`
补跑。能源收集屏障 `ENERGY_PRECACHE_COLLECTION_REQUESTED.json` 在 RPM 补跑归档后
继续阻止方法提前启动；能源协调进程合并结果、移走自身屏障，再自动恢复 SkillOpt、
Trace2Skill。旧失败轨迹保留，不将原评分超时伪造为 0 分。

2026-11-16，首批 SkillOpt 复测的 OpenHands 安装遇到中科大 wheel 下载持续
返回 429，改用已核验相同版本文件 SHA256 的阿里云镜像。
`installer_mirror.py` 只识别原生 OpenHands 固定 Git 提交及 SDK/tools 1.22.1
约束的 `uv tool install`，为这一次调用设置下载索引，保留所有原参数。
其他 uv 命令直接交给原二进制；安装成功后原二进制按 SHA256 核对并原子恢复。
容器、下载缓存、模型配置及原生任务预算继续沿用。

本轮运行目录的 `live_installer_mirror.py` 在 Agent 启动前应用此恢复，
读取 `INSTALLER_MIRROR_RECOVERY_REQUESTED.json`，将原始 uv 哈希、安装重试及
恢复记录保存到 `installer-mirror-recovery/`；既有 Python 版本清单继续记录
实际安装包。控制器持有独占锁，重启时会识别已有路由，避免重复包装。
恢复控制器可由本机 methods Python 单独启动，运行目录中的源码哈希必须与请求一致。
它只调整 OpenHands 安装下载，不修改任务构建或评分器的默认源。
