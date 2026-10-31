# 本机 OpenHands + DeepSeek

OpenHands 源码的独立环境已安装于 `harness/openhands/.venv`，使用 Python 3.12，CLI 1.16.0、SDK 1.28.1。使用上游锁文件安装：

```bash
cd harness/openhands
uv sync --locked --no-dev --python 3.12
```

临时连接验证脚本已删除。后续通过统一的 harness/诊断后端接口接入方法，不依赖该脚本。已安装的独立环境及验证记录保留。

验证记录在 runs/openhands-deepseek-smoke/：API 最小请求成功；真实 Agent 使用 FileEditorAction 的 view 操作读取工作目录中的 check.txt，随后 FinishAction 返回文件中的标记。尚未连接 Stage1。


## 本地修复：自动历史压缩事件的 JSON 输出

CLI 1.16.0 的 `json_callback` 使用 Python 模式 model_dump，Condensation 事件的 forgotten_event_ids 保持为 set，json.dumps 因此抛出 TypeError，导致会话终止。已改为 model_dump(mode="json")，保留正常历史压缩，不关闭压缩也不扩大上下文窗口。

本地修复保存在 harness/patches/openhands-json-condensation.patch，附有针对真实 Condensation 事件的回归测试。SDK 安装目录未修改。原始失败会话保留；使用副本恢复会话进行验证，不改原始 context.json。
