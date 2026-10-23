# EviSkill 原生执行器

- `alfworld_runner.py`：ALFWorld 执行循环。
- `scienceworld_runner.py`：ScienceWorld 执行循环及环境服务连接。
- `appworld_runner.py`：AppWorld 执行循环及环境服务管理。
- `agents.py`：动作模型与提示组织；LLM client 通过构造参数注入。
- `prompts/`：原始 Agent 提示依赖，保留上游布局。
- `scripts/serve_scienceworld_env.py`：ScienceWorld 环境服务入口。

保留 `run_task()`、`run_task_set()` 及原始结果字典接口，需要调用方传入 action_agent 和 skill_bank。这三个 Runner 尚未包装成统一的 RunBackend/ExecutionAdapter，不能直接传给现有 Executor。

真实运行仍需单独安装对应环境、配置数据路径；HTTP 环境连接使用 requests，ALFWorld 使用其原有 Python 环境与配置，ScienceWorld 需要相应 Java/环境依赖，AppWorld 可指定环境服务 Python。不复制数据、虚拟环境或凭据，不自动启动服务。
