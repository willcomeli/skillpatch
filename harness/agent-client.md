# 公共 AgentClient

`harness.agent_client.AgentClient` 统一调用本机 harness，会话职责与 Stage1/2/3 的业务逻辑分离。

```python
from harness.agent_client import AgentClient, AgentConfig

result = AgentClient().run(
    AgentConfig(model="openai/deepseek-flash", base_url="https://api.deepseek.com"),
    prompt=stage_prompt,
    material_paths=[context_path],
    output=new_session_dir,
    environment=environment,
)
```

输出为 AgentResult：status、output_dir、report_path、final_text、return_code、errors。配置用 api_key_env 指定凭据变量名；command 可以替换本地 CLI 可执行入口。仅支持 OpenHands headless JSON 协议，不把其他程序冒充支持的 harness。

材料保持为文件路径，不插入整个上下文正文。客户端将 prompt 和材料路径写入会话 prompt.md，要求 Agent 输出到 workspace/report.json；report_name 可供其他阶段自定义。日志和会话写入 output，使用独立的持久化目录及环境覆盖。

目录必须新建。时间预算为整个子进程会话，超时或取消停止进程组。零退出码仍需排除会话错误并确认报告和终结回答。日志执行已知密钥脱敏，不保存 Authorization 请求头。

本机模式不创建 Docker，也不是文件访问沙箱。阶段提示负责规定材料范围，阶段程序负责输出校验和输入完整性核验。
