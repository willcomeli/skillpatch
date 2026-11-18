# EviSkill 复现：ScienceWorld 清单来源核对

## 找到的公开来源

SkillNet 官方仓库的
[ScienceWorld 数据目录](https://github.com/zjunlp/SkillNet/tree/edfbda23ac2108b59579300d050218558d20f6a6/experiments/src/scienceworld/data)
提供了以下文件。原文件已原样保存到 `raw/skillnet/`，下载 URL、SHA-256 和核对结果
记录在该目录的 `provenance.json`。

| 公开文件 | 数量 | 每条记录的格式 |
| --- | --- | --- |
| `train_indices.json` | 1483 | `[task_name, variation_idx]` |
| `valid_indices.json` | 194 | `[task_name, variation_idx]` |
| `test_indices.json` | 211 | `[task_name, variation_idx]` |
| `max_steps.json` | 24 个任务族 | 任务名到建议步数的映射 |
| `taskname2id.json` | 29 项 | 任务名到编号的映射 |

固定来源版本：`edfbda23ac2108b59579300d050218558d20f6a6`。

## EviSkill 使用这些数据的证据

EviSkill 参考版本为 `896dbcb75b94d09dc5330e1cc6dcadb864244e77`。
其分集脚本声明来源为 SkillNet-derived manifests，并按照 `skillnet_index` 选取任务。
把这些索引解释为 SkillNet 对应索引文件中从 0 开始的行号，得到：

- train：96 条，24 个任务族，每族 4 条。
- val：48 条，24 个任务族，每族 2 条。
- test：保留 211 条。
- 最终三个集合按 `(task_name, variation_idx)` 检查，两两交集均为 0。

例如 train 索引 `1386` 对应
`["task-6-lifespan-(shortest-lived)", 6]`；val 索引 `154` 对应
`["task-4-grow-fruit", 80]`。

数量和覆盖情况与 [EviSkill 论文附录 B.1](https://arxiv.org/html/2610.05030v1#A2.SS1)
一致。这支持它们是还原任务身份的上游来源，但不证明下面的包装文件已逐字节复原。

## 仍未公开找到的中间产物

EviSkill 的 `build_scienceworld_split.py` 并不直接读取上述二元数组，
而是要求已经包装好的 `train.json / dev.json / test.json`：
记录至少包含 `skillnet_index`、`task_family` 和执行任务所需字段。
脚本还固定了这三个包装文件的 SHA-256。

检查官方当前 main 文件树、已有本地 Git 历史、releases 和 issues 后，
尚未找到这三个包装文件、确切的转换脚本或它们的直接下载地址。
不能把 `train_indices.json` 直接改名为 `train.json` 后交给现有脚本。

当前可确认的链路是：

```text
SkillNet 公开的任务/variation 数组
    → EviSkill 所用的包装清单（中间转换步骤未找到）
    → 固定 skillnet_index 选择 train96 / val48，保留 test211
```

SkillNet 的 `experiments/scienceworld_run.py` 使用 `simplificationStr="easy"`；
这说明 SkillNet 自身的设置，不能单独据此证明 EviSkill 包装清单里的设置完全相同。

## 已生成的重建清单

公共构建脚本现支持 `--source-format skillnet-indices`，不再依赖缺失的中间包装文件。
当前已生成 train 96 / val 48 / test 211 个任务对象，并校验源文件哈希、固定索引
顺序、逐族数量以及最终集合无交叉。记录了如下明确选择：

- `simplification="easy"`：来自 SkillNet 自身的公开运行设置。
- `recommended_max_steps`：来自 SkillNet 的 `max_steps.json`。
- `task_id`：本项目生成的 split 加源行号标识，不宣称复原作者的任务 ID 字符串。

这些信息和限制均记录于 `split_manifest.json` 的 `reconstruction` 中。

严格复现仍应向 EviSkill 作者索取三个包装文件，或确切转换脚本及来源版本，
再使用原始包装文件的 SHA-256 校验。当前重建清单已经记录为 reconstructed，
仍需核对 simplification、任务命名、步数等字段，
不能静默关闭哈希校验后声称与原始包装文件完全一致。
