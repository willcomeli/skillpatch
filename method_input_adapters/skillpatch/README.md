# SkillPatch 诊断适配器

将四个数据集的执行结果适配为 SkillPatch 使用的 `DiagnosisContext`，不改变各 Runner 的执行输出，不调用模型。

| 数据集 | 适配器 | 输入 |
| --- | --- | --- |
| SkillsBench | SkillsBenchDiagnosisAdapter | 已结束的 Executor 运行目录 |
| ALFWorld | AlfWorldDiagnosisAdapter | Runner 原生单任务 JSON 文件，或含 result.json 的目录 |
| ScienceWorld | ScienceWorldDiagnosisAdapter | 同上 |
| AppWorld | AppWorldDiagnosisAdapter | 同上 |

```python
from pathlib import Path
from method_input_adapters.skillpatch import build_diagnosis_context
from context import render_diagnosis_context

context = build_diagnosis_context(Path("runs/appworld/tasks/task_1.json"))
text = render_diagnosis_context(context)
```

原生结果通过 `dataset` 分派；SkillsBench 继续通过 `benchmark` 分派并检查完成状态。批次列表、JSONL 和运行中检查点不是单任务输入，需要调用方逐任务提供已保存结果。

原生映射：instruction → Q，retrieved_memory_text → S，trajectory 每步 → T，success/score/evaluation_summary 等 → F。保留每步原始字段，以 JSON pointer 记录来源。不读取原始检索对象替代实际交付的 Skill 文本，不把 Skill ID 伪装成文件路径，不将评分缺失视为零，不用成功标记证明轨迹完整。源 JSON 的哈希不能替代独立的输入快照核验；这些限制记录在 limitations。仅收集白名单最终反馈，不把训练要求标签自动放进诊断输入。

具体适配器与协议统一放在 adapter/：base.py、skillsbench.py、alfworld.py、scienceworld.py、appworld.py。
