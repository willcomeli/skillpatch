# AppWorld 原任务补丁重跑

消费 Stage2 的 `generation/patch.json`，使用原执行存档中的模型设置、Runner 参数、单任务引用和 Skill 快照，从初始环境重新执行一次原任务。保留已有侧车补丁，并追加本次候选。原执行结果作为比较基线，不额外重跑基线，不执行 dev 验证集，不调用模型效果评价，也不自动接受或激活补丁。

在项目根目录执行准备检查：

```bash
.venvs/methods/bin/python -m set_up.cli validate-appworld-patch \
  --patch /path/to/stage2/generation/patch.json \
  --output runs/appworld-patch-preview --prepare-only
```

正式重跑使用新的输出目录：

```bash
.venvs/methods/bin/python -m set_up.cli validate-appworld-patch \
  --patch /path/to/stage2/generation/patch.json \
  --output runs/appworld-patch-replay --env-file .env
```

也可调用 `pipeline.run_validation(patch_file, output, environment, prepare_only=False)`。

输入必须保留 Stage2、Stage1 和原执行存档。Stage1 来源必须是原生入口 `run` 保存的 `tasks/task_*.json`；仅有独立任务结果而没有 `config.json`、`input/tasks.json` 和 `input/SKILL.md` 时，无法恢复执行设置，会明确报错。批量存档也只重跑触发该补丁的任务。

输出：

- `plan.json`、`config.json`：来源、执行参数、原任务评分和重跑计划。
- `binding.json`：诊断快照目标到保存的原始 Skill 的绑定记录。
- `candidate_skill/`：原始 Skill 与完整侧车补丁对。
- `candidate/`：原生执行输出；单任务 JSON 中含轨迹、官方评分与补丁交付记录。
- `comparison.json`：前后成功状态、评分、差值及轨迹路径。仅正式重跑且评分有效时生成。
- `result.json`：`prepared`、`completed`、`failed` 或 `cancelled`；`acceptance=not_decided`。

`completed` 表示重跑与评分有效，不表示任务修复。对比结果区分 `repaired`、`still_failed`、`regressed`、`still_succeeded`；执行异常或官方评分无效记为 `failed`，不充作任务零分。历史执行、诊断和生成存档不修改，输出目录不得复用。

存档完整性检查保存普通文件的内容摘要和软链接的目标文本。原生 AppWorld 运行目录中的数据集软链接可以保留，检查不会遍历其指向的外部数据；重跑期间改变链接目标仍会触发错误。Skill 补丁目录继续禁止软链接。

部署前核对原任务与 Stage1 原始 context、实际交付文本及补丁来源。AppWorld 诊断目标来自交付文本快照，因此部署时将唯一引文绑定到保存的原始 `SKILL.md`，原文版本和路径另存于 `binding.json`。不支持指向已有补丁内容的目标；已有补丁时不支持包含补丁文本的 whole_skill 目标。重复引文仍受现有 Stage2 的唯一性检查约束。

历史原生存档尚未锁定外部数据集内容、AppWorld 安装版本及通过环境变量解析的服务地址；重跑应使用相同本机环境和 `.env`。单任务重跑结果不能证明补丁泛化能力。此入口不实现实验轮次、补丁接受策略或 Stage3 整合循环，后续由实验编排调用。
