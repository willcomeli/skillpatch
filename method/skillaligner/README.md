# SkillAligner

根据 [SkillAligner: Treating Retrieved Skills as Adaptable Drafts at Execution Time](https://arxiv.org/pdf/2608.06880)（2608.06880v1）独立复现的方法核心。未使用作者源码。

## 方法与代码对应

| 论文 | 实现 |
| --- | --- |
| §3.1，Eq.1：H = SkillAligner(q, S_q, E) | `core.adapt(query, specification, skills, output, client, model=...)` |
| §3.3：任务约束提取、片段绑定、保留/压缩/删除 | `prompt.txt` 的 Task grounding，模型在一次联合调用中完成 |
| §3.4：接口匹配、语义保持的修复、Direct/Guarded/Abstain | `prompt.txt` 的 Execution-environment alignment，以直接步骤/条件检查/省略体现 |
| §3.5：依赖排序、冲突消解、重叠合并 | `prompt.txt` 的 Skill composition |
| 附录 A.5：BM25 top-5 | `core.retrieve`；也可直接传入固定的已检索候选 |
| 附录 A.6，Figure 4：一次结构化调用 | `prompt.txt` 转录公开模板，`core.messages_for` 注入三个输入，`core.adapt` 发起一次调用 |
| 四字段指南 | `core.validate_guide` 严格验证 Primary/Checks/Avoid/Fallback 字符串列表 |
| Eq.2：后续轨迹固定使用 H | 适配层负责将同一份指南交给共享执行器；核心不执行任务 |

论文 §3.2 与附录 A.5–A.6 明确三个阶段联合在一次调用中完成，不构建或存储运行时中间组合图。因此这里不把 Figure 2 中的概念步骤改为三次模型请求，也不额外要求模型输出任务合约、片段类别或关系图。模型返回最终指南。

本目录不导入 `method_input_adapters`、`excutor` 或 `set_up`。模型客户端通过参数注入，核心可脱离项目执行器使用：

```python
from method.skillaligner import adapt

guide = adapt(query, execution_specification, retrieved_skills,
              output_dir, client, model="your-backbone")
```

亦可在仓库根目录独立运行：

```bash
.venvs/methods/bin/python -m method.skillaligner \
  --input task.json --output runs/skillaligner-method/task --model <模型名>
```

`task.json` 为含 `query`、`execution_specification`、`retrieved_skills` 的对象。技能为 `id/name/text/description` 记录列表；可用 `skill_library` 替代 retrieved_skills，启用 BM25。客户端读取 `METHOD_API_KEY` 和 `METHOD_BASE_URL`。

## 复现边界

BM25 分词及参数、4096 输出 token 上限、OpenAI-compatible JSON transport 是明确的工程选择。原技能库快照和 benchmark-specific specification 全文不可由论文恢复，需提供实际公开输入。默认 temperature=1、top_p=1，与附录 A.3 一致；若服务支持，应在服务端关闭 thinking。

这是一份方法实现，尚未复现原论文 ALFWorld/WebShop/SearchQA 的实验分数。已有 SkillPatch 执行器的接入说明见 `../../method_input_adapters/skillaligner/README.md`。
