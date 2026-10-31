# 命令行运行入口

在整理版根目录执行：

```bash
python -m set_up.cli prepare --config configs/example.toml --output runs/preview
python -m set_up.cli run --config configs/example.toml --output runs/actual --env-file /path/to/.env
```

配置中的 `task.benchmark` 选择数据集，不通过 patch 参数选择 Runner：

| benchmark | 实际执行路径 | 可选补丁参数 |
| --- | --- | --- |
| skillsbench | 现有 Executor + SkillsBenchExecutionAdapter + BenchFlowBackend | --patch-skills-dir：完整多 Skill 包 |
| alfworld | 原生 AlfWorldRunner | --patch-skill-dir：一个 Skill 目录 |
| scienceworld | 原生 ScienceWorldRunner | 同上 |
| appworld | 原生 AppWorldRunner | 同上 |

不指定补丁参数，执行原 Skill；指定补丁参数则追加经过版本核验的补丁。两个补丁参数互斥，不适用当前数据集的参数会明确报错。

SkillsBench 原有 TOML 格式不变。三个原生数据集的配置样例位于 `configs/examples/`，占位路径需要替换。`task.path` 是单任务引用或任务引用列表的 JSON；原生 Runner 接受的任务引用格式保持原样，例如 AppWorld 使用 `{"task_id":"a30375d_1","split":"test_normal"}`，ScienceWorld 使用包含 task/variation_idx 的任务引用。ALFWorld 可使用其任务编号或 gamefile 引用。

原生配置：

- `task.skill_path`：原始 Markdown Skill 文件。无 Skill 时删除此字段，改为 `skill_mode="no-skill"`。
- `agent.name="native"`：采用原生动作 Agent，不是 OpenHands。
- `agent.model`、`api_key_env`、`base_url_env`：模型和凭据环境变量名。URL 是 Chat Completions-compatible API 的根路径，例如带 `/v1` 的地址；客户端追加 `/chat/completions`。也可设置 agent.base_url。
- `runtime.runner_options`：所选 Runner 构造函数的关键字参数；相对路径以 TOML 所在目录为基准。AppWorld 的 output_dir 由入口统一管理，不能覆盖。
- `runtime.timeout_sec`：每次模型 HTTP 请求的时限；任务步数通过 runner_options.max_steps 控制，不承诺原生任务整体墙钟超时。
- `agent.max_retries`：原生模型请求的临时错误重试次数，默认 0。仅重试连接错误、超时和 HTTP 408/429/500/502/503/504；不重试认证失败、格式错误或截断响应。重试采用最多 4 秒的指数退避，实际请求与重试次数保存在批次 `result.json` 的 `model_requests` 中，原任务重跑继承相同设置。

prepare 不连接模型、不启动环境服务，只核验任务列表、Skill、补丁及构造参数，保存输入快照和 plan.json；不能证明环境安装或凭据有效。run 才创建模型客户端、Agent 和环境。当前 launcher 串行执行任务，异常时保留已经保存的结果并关闭 Runner。

原生输出：result.json 是本轮批量状态；单任务原生结果位于 tasks/task_000001.json 等。调用诊断适配器时传入单任务文件，不传入批量状态文件。它们仍是原生结果，没有转换成 ExecutionResult。

诊断及 Stage2/Stage3 命令尚待后续恢复和接入，本次不改变其迁移范围。

SkillsBench 不再设置额外的整轮执行墙钟时限，执行和验证时限由任务集及 BenchFlow 管理。
历史 SkillsBench `runtime.timeout_sec` 可读取但被忽略，新配置无需填写；原生执行器的同名设置仍表示模型 HTTP 请求时限。

AppWorld 已生成补丁的原任务重跑入口为 `validate-appworld-patch`，支持 `--prepare-only`。
它不运行 dev 集、不自动接受补丁；调用与输入要求见
[AppWorld 补丁重跑说明](../method/skillpatch/patch_validation/appworld_patch_validation/README.md)。

共享初始轨迹采集和 SkillPatch 单轮改进脚本位于 `scripts/appworld/`。
`collect_trajectories.sh` 生成可复用的冻结轨迹清单，`skillpatch/smoke.sh` 和
`skillpatch/experiment.sh` 分别使用小规模和全部清单输入执行同一流程。
参见 [AppWorld 脚本使用说明](../scripts/appworld/README.md)。

三种方法的完整连通测试入口为 `scripts/appworld/smoke.sh`（`python -m set_up.appworld_smoke`）。
它复用同一冻结轨迹，运行 Trace2Skill、SkillOpt、SkillPatch 的实际改进及候选执行，
保留前两种方法的官方 dev 验证，只有 SkillPatch 跳过 dev；是否提高得分不影响连通判定。

统一测试和全量入口支持独立的 `--optimizer-config`，默认使用
`configs/appworld/optimizer.toml`：方法模型读取根目录 `.env` 的 `METHOD_MODEL`、
`METHOD_API_KEY`、`METHOD_BASE_URL`；执行模型独立读取 `EXECUTION_MODEL`、
`EXECUTION_API_KEY`、`EXECUTION_BASE_URL`。两种角色都可以更换兼容模型；
验证和重跑继续使用冻结轨迹的原生配置。单独采集或续采不需要方法模型凭据。

SkillPatch 诊断与生成支持 `openhands` 和 `codex` 两种 harness。
统一入口从 `.env` 的 `SKILLPATCH_HARNESS` 选择（当前为 `codex`），
独立 Stage1/Stage2 配置则在 `[diagnosis]` 中设置 `harness`。
Codex 使用显式方法模型 provider 和 Responses API，不依赖本机 Codex 登录凭据。
