# Stage3 初版验收（2026-10-13）

已实现补丁集合的对象定位、去重、按需整合、保持效果复验、版本登记及执行部署。原始 Skill、配套资源、既有诊断与 Stage2 预算不改写。多条真实补丁的合并效果仍待实验。

## 实现范围

- `patch_targets.py` 按明确 Skill 身份、完整原始资源摘要和结构边界生成稳定对象 ID。不同引文只有归属同一原始对象时才能合并；模糊或跨界定位不猜测。
- `stage3_sources.py` 重新读取真实 Stage2 队列和完整证据，核验候选、成对执行条件、独立评价与实际评分比较。未决评价不能直接继承，历史报告通过新增映射关联到原始对象。
- `stages/consolidation.py` 原样保留首条补丁，精确去重；已有集合添加新内容时复验全部相关来源。整合模型仅输出 keep／merge／defer，只有 merge 生成新的两部分补丁正文。
- `stage3_validation.py` 与 `stages/consolidation_evaluation.py` 采用新的成对执行和独立评价检查修复保持；允许评分持平，证据不足时待验证，退化时不接纳。
- `patch_sets.py` 保存固定原始资源、不可变版本和追加式生效记录；原子切换指针，核对预期父版本，支持导出与回退。
- `patch_deployment.py` 经统一 adapter 将指定版本部署到任务副本。执行器在 adapter 和提示准备后核对实际 Skill 包，摘要不匹配时阻止后端启动。

Stage3 核心不依赖 SkillsBench 任务名或 BenchFlow SDK。独立 benchmark 替身用于工程测试，目前实际实验仍只有 SkillsBench。

## 实际 Excel 登记与部署准备

输入是已验证补丁 `P5b578f810c3a114d-r2`，来源为 [Stage2 队列](runs/stage1-focus4-bsR4Mf/exceltable-in-ppt/stage2/validation-20261013T054645Z-44acacfc/stage3_queue.json)。原对照基线通过 6/8 项测试，候选通过 8/8 项，独立评价及产物检查支持原 under_use 修复。

本轮只重新核验该证据、登记集合、检查重复队列并准备执行输入，**新增模型调用 0 次，新增后端任务 0 次**。没有将本轮登记称作新效果实验。原 Stage2 回收记录仍保留：外层协调进程中断后从已经完成的 BenchFlow 产物恢复，无法恢复的退出码保持未知，未重写为正常退出。

| 项目 | 结果 |
| --- | --- |
| Skill 身份 | `skillsbench/reference/xlsx` |
| 对象 | 原始 xlsx 的第 5 步，稳定 `target_id` 保存于映射记录 |
| 首次处理 | completed / accepted / keep，`reason=stage2_inheritance` |
| 生效版本 | `v-f13993147e2efbf5142ddfd8c9498d799c5d360cef0df95e6340a988254d0435` |
| 重复队列 | unchanged，无重复补丁或新版本 |
| 原始单个 Skill 摘要 | `sha256:85aea994a1fb960c8a364dd04912766a917203c8b2dde0189a86ac9944111258` |
| 呈现后的单个 Skill 摘要 | `sha256:76780adc5bce8046a55dc7856c7a73fe9fda2d1e1fb7bb0dc43a9c75baadf280` |
| 实际完整 Skill 包摘要 | `sha256:1b22494911b1c42438d3fca076465f8cb168c37c6583a872777f94e132ea6483` |
| 历史与资源保护 | 原任务、原始 Skill、已核验历史证据、Stage2 预算均未变化 |
| 其他任务内容 | pptx 等其他 Skill 以及非目标任务资源逐文件一致 |
| 执行准备 | prepared，实际资源摘要 verified，尚未启动任务 |
| 回归覆盖 | not_configured；未验证跨任务保持与迁移 |

对应记录：

- [首轮预检](checks/20261013-stage3/prepare/result.json)、[正式登记](checks/20261013-stage3/register/result.json)、[对象映射](checks/20261013-stage3/register/target_binding.json)。
- [重复队列](checks/20261013-stage3/repeat/result.json)、[执行准备](checks/20261013-stage3/deployment/result.json)、[实际部署核验](checks/20261013-stage3/deployment/skill_deployment_verification.json)。
- [验收汇总](checks/20261013-stage3/verification.json)、[操作前原始内容与证据摘要](checks/20261013-stage3/immutability-before.json)。

本轮登记命令如下；输出目录现已存在，不应复用：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
/home/lihanlun123/workspace/dataset/skillsbench/.venv/bin/python -m skillpatch stage3 \
  --queue runs/stage1-focus4-bsR4Mf/exceltable-in-ppt/stage2/validation-20261013T054645Z-44acacfc/stage3_queue.json \
  --skill-key skillsbench/reference/xlsx \
  --registry patch_sets \
  --config configs/stage3.toml \
  --output checks/20261013-stage3/register
```

执行准备使用 `prepare --config configs/reference-full/exceltable-in-ppt.toml --patch-registry patch_sets --patch-skill-key skillsbench/reference/xlsx --output checks/20261013-stage3/deployment`，没有执行 `run`。

## 工程验证与限制

最终完整测试 **504 项通过，耗时 18.597 秒**，见 [原始测试输出](checks/20261013-stage3/tests.log)。最终代码重新读取真实 Excel 队列和已登记证据，来源、原任务及 Stage2 预算哈希仍一致。

工程测试使用临时资源、模拟模型与后端，覆盖：

- 同一步不同引文、whole_skill／substep 区分、围栏与重复文本、已有补丁引文的来源追溯。
- 原始资源固定、精确呈现、版本完整性、并发父版本变化、导出与回退。
- 已接纳来源重新核验、证据变化、执行条件不一致、评价仍有不确定性或要求修订。
- 首补丁登记、精确去重、重复队列、额外配置回归任务、旧集合队列及多来源合并。
- 新来源任务成功但旧来源修复退化时拒绝；评分持平且已有修复保留时可以接纳。
- 已有证据目录不能被输出污染；adapter 忽略部署或提示准备修改 Skill 时阻止执行；成对任务后端版本不一致时待验证并跳过评价。

完整测试命令：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
/home/lihanlun123/workspace/dataset/skillsbench/.venv/bin/python \
  -m unittest discover -s tests -q
```

本轮没有同一对象的第二条真实补丁，因此实际登记未触发整合模型或新任务复验。模拟检查不能证明真实多补丁合并质量、重复稳定性、misuse 改善或跨 benchmark 泛化。相关成功任务目前未配置，覆盖缺口随补丁集合保留。自动演化调度、批量队列管理和中断后续跑不属于本版入口。

## 2026-10-14：移除不确定说明拦截

按用户要求，评价删除 `uncertainties` 字段及对应拦截；证据不足的 `uncertain` 状态和修订请求检查保留。旧报告导入时仅在内存中忽略废弃字段，不改写档案。最新完整测试为 **578 项通过，19.607 秒**。lake-warming-attribution 与 offer-letter-generator 的已有 Stage2 队列均通过只读导入预检，尚未由本次修改登记或激活。详见 [Stage2 验收记录](Stage2验收.md)。上文 504 项测试及旧字段拦截描述属于首次验收时点。

## 2026-10-14：整合与评价按目标读取轨迹

按用户确认，整合模型的逐来源校验和保持效果评价均取消“引用行为之后的全部轨迹必须完整展示”的要求。保留每次执行记录完整性、来源编号隔离、原 Skill 上下文及实际展示的精确行为引文检查；模型通过搜索和按需读取检查相关后续修正、撤销、等效实现与最终结果。各阶段原有的接纳标准保持不变，关键证据不足仍待验证。此轮完整测试 **587 项通过，19.479 秒**；真实历史输出的离线引用校验见 [Stage2 验收记录](Stage2验收.md)，未启动新的模型会话或任务运行。
