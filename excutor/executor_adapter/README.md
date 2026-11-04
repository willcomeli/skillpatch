# Benchmark 执行架构

三个 benchmark 的 Runner 包与 `excutor_runners/` 同级，均直接位于 `excutor/` 下。
`executor_adapter/` 负责适配器、参数校验与注册分发；`runner_common/` 存放共用运行组件。

```text
excutor/
├── excutors.py
├── excutor_runners/       # 现有外部执行后端
├── appworld_runners/
│   ├── runner.py         # AppWorld 环境连接、执行和评测
│   ├── agent.py          # Python 动作与对话组织
│   ├── prompts.py        # 提示词及配套官方 ReAct 文本
│   └── manifests.py      # 官方数据划分和任务清单
├── alfworld_runners/
│   ├── runner.py
│   ├── agent.py
│   ├── prompts.py
│   └── task_family.py
├── scienceworld_runners/
│   ├── runner.py
│   ├── agent.py
│   ├── prompts.py
│   ├── task_family.py
│   └── scripts/serve_scienceworld_env.py
├── runner_common/
│   ├── launcher.py       # 模型客户端、输入快照、Skill 加载、结果保存和清理
│   ├── agents.py         # Agent 基类和通用动作解析
│   ├── action_runtime.py
│   ├── model_errors.py
│   ├── json_utils.py
│   ├── bundles.py
│   ├── prompt_common.py  # 上游共用提示定义
│   ├── prompts.py        # 上游跨 benchmark 提示选择兼容接口
│   └── task_family.py    # 上游跨 benchmark 任务族选择兼容接口
└── executor_adapter/
    ├── appworld.py       # AppWorldAdapter，绑定 Runner、Agent，管理运行输出目录
    ├── alfworld.py       # AlfWorldAdapter
    ├── scienceworld.py   # ScienceWorldAdapter
    ├── native.py         # NativeBenchmarkAdapter，参数校验与 Runner 构造
    ├── skillsbench.py    # SkillsBenchExecutionAdapter
    ├── base.py
    ├── models.py
    ├── storage.py
    └── registry.py
```

## 原生执行流程

```python
from excutor.executor_adapter.registry import get_native_adapter
from excutor.runner_common.launcher import run_native

adapter = get_native_adapter("appworld")
options = adapter.runner_arguments(config, output)  # 仅校验，不启动环境
result = run_native(config, output, environment, prepare_only=True)
```

`run_native()` 通过 registry 选择 adapter，再由 adapter 创建对应 benchmark 包中的
Agent 和 Runner。共享启动流程负责模型请求重试、输入快照、补丁 Skill 预检、
逐任务结果保存、凭据脱敏和 Runner 关闭。各 Runner 包只依赖自身实现与共享组件，
加载一个 adapter 不会加载另外两个 benchmark。

原生 Runner 的 `run_task()`、`run_task_set()` 和结果字典保持兼容，官方环境评测仍由
各自 Runner 完成。原有 TOML 中的 `runtime.backend="eviskill"` 保持可用。

## 与 SkillsBench 的接口区别

`get_native_adapter()` 返回 `NativeBenchmarkAdapter`，用于构造直接与环境交互并评测的
原生 Runner。`get_execution_adapter("skillsbench")` 返回 `ExecutionAdapter`，提供
`prepare()` 和 `evaluate()`，配合外部 `RunBackend` 使用。两种执行契约保持兼容；
原生 adapter 不直接传给 `Executor`。

真实执行仍需要单独安装相应环境及数据；此次结构调整不安装环境、不启动服务。
