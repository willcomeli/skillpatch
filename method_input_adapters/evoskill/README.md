# EvoSkill 方法适配器

接入 `method/evoskill` 固定的原生 `SelfImprovingLoop`，不修改上游源码、提示词或演化规则。

```text
已有任务划分 → train_pools / val_data（test 单独保留）
原生循环 → SharedExecutionAdapter.run → SharedExecutorBridge → 现有 executor
执行归档 → 官方 success / score + 全部保存事件 → 原生 AgentTrace
原生 proposer → 原生 generator → .claude/skills/ → 下一轮执行
官方评分 → 原生候选集合选择与 Git 检查点
```

## 输入与交付

任务清单与其他方法共用 `execution.load_tasks`：每条包括唯一 `id`、`split`、执行 `config`，
原生数据集使用 `task_ref`。`create_adapter` 检查任务身份不能跨集合重复，不重新划分数据。
EvoSkill 提交给执行 Agent 的 question 是清单 ID，轨迹中保留实际任务说明。

方法生成的 `.claude/skills/` 是当前候选的完整 Skill pack，可以从空目录开始。
SkillsBench 整包交付，包括参考文档、脚本及其他资源；不会重新混入已删除的旧 Skill。
ALFWorld、ScienceWorld、AppWorld 使用现有 Runner 的文本 Skill bank，按相对路径将所有
`.md` / `.txt` 文件全文注入；含脚本、二进制资源的候选明确报错，不能声称文件系统交付等价。
快照禁止符号链接，以保持候选资源和保存哈希的一致性。

`score_mode` 必须由实验脚本指定：`hard` 使用官方 success，`soft` 使用官方归一化 score。
不把基础设施错误或缺失评分转换为零分。原生循环的失败采样阈值（0.8）、类别采样、
候选集合及停止条件继续由上游实现和 `LoopConfig` 控制；适配器不自行改变它们。

由于环境任务没有可供答案字符串比对的 ground truth，`AgentResponse.final_answer` 放的是
适配器生成的评分回执 ID。`scorer` 仅接受该任务执行后登记的回执，从官方反馈取分，
不会信任模型提供的分数。真实任务、逐步观察、动作、返回 thought、反馈均保留在
`AgentTrace.result/messages`；thought 不被解释为经过验证的内部推理。不生成轨迹完整性标签。
当前公共归档没有费用与 token 数据，因此 usage 标记 available=false，cost 占位为 0，
不能将上游显示的合计费用理解为完整实验真实费用。

## 已有轨迹转换

无需凭据或启动环境：

```bash
.venvs/methods/bin/python -m method_input_adapters evoskill-export \
  /path/to/native-task.json /path/to/skillsbench-task-run \
  --output runs/method-adapters/evoskill-export
```

输出 `traces/*.json`（真实上游 AgentTrace schema）和 `adapter_manifest.json`。
完整保留已保存事件；没有推测或补造未保存的模型协议。原执行归档不变。
这是离线输入检查，不会将旧轨迹直接冒充新候选的执行结果。

## 接入实验脚本

调用过程如下；`config` 是实验脚本明确构造的原生 `LoopConfig`，
`method_agents` 是使用上游 agent profiles 构造的 `LoopAgents`。

```python
from pathlib import Path
from method_input_adapters.evoskill import create_adapter
from method_input_adapters.evoskill.upstream import load_module

# workspace 必须是独立初始化并有初始提交的 Git 仓库。
# 初始 Skill pack 放到 workspace/.claude/skills/；空目录表示从零开始。
workspace = Path('/path/to/isolated-evoskill-program')
adapter = create_adapter(
    manifest, workspace, execution_output, environment,
    model=execution_model, score_mode=experiment_score_mode,
    concurrency=execution_concurrency,
)
manager = load_module('registry').ProgramManager(cwd=workspace)
loop = adapter.build_loop(config, method_agents, manager)
result = await loop.run()

# 最终测试由脚本显式发起；不会进入训练池或候选验证。
manager.switch_to(result.best_program)
_, _, test_data = adapter.data()
test_traces = [await adapter.run(task_id) for task_id, _, _ in test_data]
```

proposer/generator 应使用上游 `get_skill_proposer_options` /
`get_skill_generator_options`，显式传入同一个独立 `project_root` 和方法模型；
然后使用上游 `Agent` 与相应 response schema 包装。SDK 与凭据配置遵循 EvoSkill 原生接口，
与任务执行配置分开。不能使用默认 agent profile 变量，因为它们可能绑定源码工作目录。
适配器只支持 `skill_only`，不悄悄丢弃 `prompt_only` 生成的系统提示词。

执行输出必须在 program workspace 外面。每次执行保存独立 Skill 快照、`trace.json`、
`receipt.json` 和原生执行产物；候选变更会重新执行。上游 ProgramManager 只管理独立
program 仓库，不能切换或提交本项目源码仓库。

适配器替换上游 `_evaluate` 的执行传输，避免其自带 1020 秒任务总时限和将异常计零分的逻辑。
并发由显式参数控制，不增加任务步数、任务总时限或输出 token 限制。单次网络请求与
连接恢复沿用执行配置；方法 Agent 自身的超时、重试及提案回退仍属于上游逻辑。
上游轨迹缓存不包含完整 executor 配置和环境身份，因此当前绕过该缓存；其 Git 候选管理和
循环检查点仍使用原生实现。循环的 continue_mode 是上游迭代恢复，不是任务中途环境恢复；
这层适配器不额外实现后台守护进程或自动重启策略。

`src` 是上游 Python 包名，可能与其他项目冲突，适配器会检查来源；实际实验建议使用独立进程。
最小适配检查使用现有 methods 环境；实际调用某种上游 harness 时仍须安装该 SDK 的依赖。
不为下载代码或离线转换而批量安装全部上游执行后端。

## 验证

```bash
PYTHONPATH=tests .venvs/methods/bin/python -m unittest test_evoskill_adapter
```

测试包括真实上游循环和 ProgramManager 的单轮集成，使用模拟模型与执行器，
验证候选变更重新执行、测试集隔离、评分回执绑定、异常传播和整包资源交付。
没有在此阶段启动真实实验或声称补丁效果。
