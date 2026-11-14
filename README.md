# SkillPatch

SkillPatch 整理版：将命令入口、共享任务执行器、方法输入适配、Agent harness 和方法实现分开组织。

## 代码结构

```text
set_up/cli.py           统一命令入口
set_up/                 配置和数据集工作流
scripts/                用 CLI 编排实验
data/scienceworld/      分集、来源和重建代码
excutor/                四个数据集的共享执行器及 Skill/补丁交付
method_input_adapters/  SkillPatch、SkillOpt、Trace2Skill 输入适配
method/skillpatch/      诊断、普通和 Boundary-Aware 补丁生成、整合及验证
method/trace2skill/     Trace2Skill 源码快照
method/skillopt/        SkillOpt 固定版本子模块
method/eviskill/        EviSkill 源码快照
harness/                公共 AgentClient、OpenHands/Codex 子模块和本地补丁
prompts/                方法提示词、ScienceWorld 动作提示词和执行提示增强
configs/                配置样例
tests/                  工程测试
context.py              SkillPatch 诊断上下文呈现和摘要
```

目录名 `excutor` 保留当前代码中的拼写。仓库包含源代码、配置、提示词、测试和 ScienceWorld 分集清单。数据环境、凭据和实验运行结果由本机准备。

## 获取与测试

只跑 ScienceWorld 时可使用 [独立安装说明](configs/scienceworld/README.md)。下列命令安装全仓工程测试和可选对比方法的依赖。

```bash
git clone --recurse-submodules https://github.com/willcomeli/skillpatch.git
cd skillpatch
uv venv .venvs/methods --python 3.12
uv pip install --python .venvs/methods/bin/python -r method_input_adapters/requirements.lock.txt
uv pip install --python .venvs/methods/bin/python --no-deps -e ./method/skillopt
.venvs/methods/bin/python -m unittest discover -s tests -q
```

已有克隆可执行 `git submodule update --init --recursive`。OpenHands、Codex 和 SkillOpt 固定在 Git 子模块记录的提交，仓库地址见 `.gitmodules`；来源信息另见 `harness/sources.json`、`method/SOURCES.json` 和执行器的 `SOURCE` 文件。

## 命令入口

从仓库根目录执行：

```bash
.venvs/methods/bin/python -m set_up.cli --help
.venvs/methods/bin/python -m set_up.cli scienceworld --help
.venvs/methods/bin/python -m set_up.cli appworld --help
.venvs/methods/bin/python -m set_up.cli skillsbench experiment --help
.venvs/methods/bin/python -m set_up.cli baselines --help
```

`prepare`、`run` 根据执行 TOML 的 `task.benchmark` 分流到 ScienceWorld、AppWorld、SkillsBench 或 ALFWorld。单条 SkillPatch 操作使用 `stage1`、`stage2`、`stage3`，再按数据集执行验证。

ScienceWorld 从采轨迹到评测的自动脚本如下。三组共享执行设置，两个打补丁策略共享 Stage 1 索引。

```bash
scripts/scienceworld/run.sh runs/scienceworld/reproduction --method all
```

`appworld experiment` 和 `skillsbench experiment` 按各自的任务协议运行所选对比方法，也可以用 `baselines` 调用 SkillOpt、Trace2Skill 的导出或演化命令。各入口复用现有执行和方法适配层。

先修改 `configs/` 中的数据集、执行器和模型路径。部分配置记录了原开发机的绝对路径，不能直接当作新机器配置。密钥通过环境变量或仓库外的 `--env-file` 提供。

具体输入、任务清单和命令参数见 [方法输入适配说明](method_input_adapters/README.md) 与 [执行配置说明](set_up/README.md)。

## 本机诊断 Agent

SkillPatch 的方法阶段目前接入 OpenHands 本机 headless JSON 会话。首次安装时应用已保存的历史压缩事件 JSON 修复：

```bash
git -C harness/openhands apply ../patches/openhands-json-condensation.patch
cd harness/openhands
uv sync --locked --no-dev --python 3.12
```

`harness.agent_client.AgentClient` 默认使用该子模块 `.venv/bin/openhands`，也可通过配置提供命令。接口见 [AgentClient](harness/agent-client.md)。配置也支持 Codex harness。

## 当前能力与边界

- 共享执行器支持 SkillsBench、ALFWorld、ScienceWorld、AppWorld；运行所需的数据集、BenchFlow、Docker 或原生环境依赖需另行安装。BenchFlow 源码副本供阅读，实际执行使用配置指定的 BenchFlow CLI。
- SkillPatch 适配器统一生成 `DiagnosisContext`。方法包括离线诊断、候选补丁生成、候选整合及 SkillsBench 补丁验证。
- SkillOpt 使用原始训练器，其任务执行及验证通过共享执行器调用。Trace2Skill 使用原始文本分析、解析和并行演化流程。
- 三个原生执行器可注入 Skill 正文和文本参考文档；可执行脚本和二进制 Skill 资源暂不支持交付。
- EviSkill 方法目前保留源码，尚未实现方法输入适配层。生成候选与正式执行评分验证是两个独立步骤。

工程测试覆盖输入转换、评分有效性、分集、候选执行与资源保留。ScienceWorld 已完成 Vanilla、普通 SkillPatch 和 Boundary-Aware 的 211 条 test 评测。其他数据集按各自环境和工作流配置。

## 实验编排与代理

- [SkillsBench 三方法实验、并发基线与超时复核](scripts/skillsbench/README.md)
- [AppWorld 实验脚本](scripts/appworld/README.md)
- [ScienceWorld 分集和 SkillPatch 运行步骤](configs/scienceworld/README.md)
- [Docker 安装代理转发与 SSH 隧道](scripts/proxy/README.md)
