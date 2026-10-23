# 独立补丁验证（SkillsBench）

接收 patch_generation 产出的完整存档，通过 SkillsBenchExecutionAdapter、Executor 和
BenchFlowBackend 执行。生成模块与验证模块互不调用；本模块不生成或自动修订 patch。

```text
生成存档中的 generation/patch.json
  → 核对来源、Stage1 context、原执行结果与快照
  → 复用原执行作为来源任务基线
  → 将 patch 写入独立 Skill 副本的补充文件
  → SkillsBench 执行器重跑候选
  → 官方评分比较 + 独立 harness Agent 评价
  → accepted / rejected / pending
```

## 命令

```bash
python -m set_up.cli validate-patch \
  --patch /path/to/generation-output/generation/patch.json \
  --config configs/skillpatch-validation.toml \
  --output /path/to/new-validation-output \
  --env-file /path/to/model.env
```

加 `--prepare-only` 会验证来源、准备补丁副本及计划，不运行任务或调用模型。
运行需要保留生成存档、Stage1 存档和最初的完整 SkillsBench 执行存档。
无需重新指定来源任务模型配置：候选沿用来源 request.json，以原执行快照作为输入。
环境中的非敏感 provider settings 必须与原存档一致；密钥值不写入存档。

`configs/skillpatch-validation.toml` 指定评价 Agent 配置、regression_configs 和 repetitions。
来源任务的多个候选重复共享同一个历史基线，不视作独立基线重复。
回归任务使用新的无补丁基线及候选，并要求基线通过；回归配置也仅支持 SkillsBench/BenchFlow。
所有 case 的目标文件必须匹配 patch 绑定的原文版本。

## 文件职责

- source.py、records.py：读取补丁来源、request 和完整执行存档。
- rendering.py、targets.py：定位所属 Skill，生成不改原文的 sidecar 补充文件。
- integrity.py、task_messages.py：检查仅目标补充文件改变；任务正文和实际消息保持一致，
  只允许经过核对的补丁读取提示前缀差异。
- comparison.py：原版 SkillsBench reward、named tests 和 passed 比较逻辑。
- evaluation.py：调用公共 harness Agent 评价原问题是否修复，保留旧版基本结构及原文引用校验。
- pipeline.py：安排基线、候选执行、评价、结果与队列保存。

补丁读取审计为 unconfirmed 时记录 warning，继续客观评分，与旧版一致。
评价调用失败时保留 uncertain 评价及错误记录，不抹掉已取得的客观评分。
输入按完整 context 文件交付，harness 自行读取；程序不声称已精确追踪所有读取范围。

## 接受规则与边界

保留旧版 objective-first 决策：全部执行检查通过，且来源比较中出现客观提升时即可接受，
即使模型评价不同意或其他指标/回归有下降；这些信息仍保留在验证记录中。
没有客观提升时，模型声称修复也不能单独接受。执行/完整性检查失败不能接受。

输出包括 plan.json、presentation/、packs/、runs/、evaluation/、validation.json 和 result.json。
接受时额外写 stage3_queue.json，记录补丁、验证文件哈希和候选包哈希，不激活任何版本。
此队列为独立验证的交接记录；目前 patch_consolidation 的 stage3 命令仍接收生成 patch，
尚未接入队列验收、注册表或版本激活，也不声称兼容旧版队列加载器。

本轮不迁移：其他数据集执行、旧版 inline 补丁呈现、自动修订循环及其生成预算管理。
需要修订时，读取 evaluation 中 revision_action，返回独立生成流程处理。
