# ScienceWorld 公共任务划分

## 当前状态

已从 `raw/skillnet/` 中的公开索引生成可执行的任务清单：train 96 条、val 48 条、
test 211 条。训练和验证任务沿用 EviSkill 的固定索引；三个最终集合按任务名和
variation 检查无重复或交叉。每条记录包含 Runner 需要的任务对象字段。

这些文件标记为 `reconstructed`，不是作者发布的原始包装 JSON。
本次显式采用 `simplification="easy"` 和 SkillNet `max_steps.json` 的逐任务步数。
它们与 EviSkill 原始包装设置的等价性仍未确认，详见 [来源核对](SOURCE_AUDIT.md)。

`pip install scienceworld` 提供 Python 环境接口和 JVM JAR，不提供该实验使用的
SkillNet 派生任务清单。分集脚本也不负责下载这些清单。

## 输入与输出

当前直接使用公开的 SkillNet 索引，无须先构造中间包装清单：

```text
data/scienceworld/
├── README.md
├── build_scienceworld_split.py
├── raw/
│   ├── README.md
│   └── skillnet/
│       ├── train_indices.json
│       ├── valid_indices.json
│       ├── test_indices.json
│       ├── max_steps.json
│       ├── taskname2id.json
│       └── provenance.json
├── train/items.json           # 脚本生成，96 条
├── val/items.json             # 脚本生成，48 条
├── test/items.json            # 脚本生成，保留原始 test 全量
└── split_manifest.json        # 脚本生成
```

从 `skillpatch/` 仓库根目录执行：

```bash
source .venvs/methods/bin/activate
python data/scienceworld/build_scienceworld_split.py \
  --source-format skillnet-indices \
  --source-dir data/scienceworld/raw/skillnet \
  --simplification easy \
  --step-budget skillnet
```

`--source-dir` 必填，指向这几个源 JSON 所在目录，不是 ScienceWorld 安装包目录。
`--output-dir` 可选，默认输出到脚本所在的 `data/scienceworld/`。

重建模式要求显式指定两个执行设置：`--simplification easy` 沿用 SkillNet 的设置，
`--simplification ""` 表示不简化；`--step-budget skillnet` 复制逐任务步数映射，
`--step-budget runner` 不写 `recommended_max_steps`，由执行 TOML 决定 Runner 预算。
这些选择会记录在 `split_manifest.json`，并不会修改 JVM 的独立步数上限。

脚本保留了 `method/eviskill/scripts/build_scienceworld_split.py` 的固定索引、
原始包装文件的预期哈希和选择逻辑：train 从原始 train 中选取 24 个任务族、每族 4 条；
val 从原始 dev 中选取 24 个任务族、每族 2 条；test 的任务身份及顺序全部保留。
这不是重新随机划分，也不等于枚举全部 SDK 任务及其 variations。

公开索引模式默认校验已固定的 SkillNet 源文件 SHA-256，再检查重建后的任务重复和
跨 split 交叉。只有独立核对任务身份、记录来源变化后，才考虑 `--allow-source-mismatch`。
原始文件和生成文件的哈希分别记录，不将重建结果宣称为原始 EviSkill 包装文件。

如果以后取得作者的 `train.json / dev.json / test.json`，原入口仍保留：

```bash
python data/scienceworld/build_scienceworld_split.py \
  --source-format eviskill-manifests \
  --source-dir /原始包装文件目录
```

## 与方法、配置的关系

所有比较方法应共享这些任务清单：

| 划分 | 用途 |
| --- | --- |
| train | 执行任务、收集轨迹，供方法更新 skill |
| val | 在预先规定的预算内比较、选择候选 skill 或补丁 |
| test | 冻结 skill 后进行最终评测，不用测试反馈继续更新 |

生成后的 `items.json` 可以直接作为 native executor 的 `task.path`。
如果使用 `method_input_adapters.execution.load_tasks`，还需包装成包含
`id / split / config / task_ref` 的统一任务清单；这个脚本不生成该包装。

执行参数与命令见 [ScienceWorld 配置说明](../../configs/scienceworld/README.md)。
