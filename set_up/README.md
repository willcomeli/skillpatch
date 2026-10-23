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

prepare 不连接模型、不启动环境服务，只核验任务列表、Skill、补丁及构造参数，保存输入快照和 plan.json；不能证明环境安装或凭据有效。run 才创建模型客户端、Agent 和环境。当前 launcher 串行执行任务，异常时保留已经保存的结果并关闭 Runner。

原生输出：result.json 是本轮批量状态；单任务原生结果位于 tasks/task_000001.json 等。调用诊断适配器时传入单任务文件，不传入批量状态文件。它们仍是原生结果，没有转换成 ExecutionResult。

方法命令已接入：`diagnose` / `stage1` 生成离线诊断报告，`stage2` 生成候选补丁，`stage3` 生成候选整合方案，`validate-patch` 使用 SkillsBench 验证已有补丁。各阶段可独立调用，具体参数通过 `python -m set_up.cli <命令> --help` 查看。

SkillsBench 不再设置额外的整轮执行墙钟时限，执行和验证时限由任务集及 BenchFlow 管理。
历史 SkillsBench `runtime.timeout_sec` 可读取但被忽略，新配置无需填写；原生执行器的同名设置仍表示模型 HTTP 请求时限。
