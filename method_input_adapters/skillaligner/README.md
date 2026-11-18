# SkillAligner 论文复现

独立实现，依据 [SkillAligner v1](https://arxiv.org/html/2608.06880v1) §3、附录 A.3–A.6。不是作者代码，也未验证论文报告的分数。

方法核心位于 `method/skillaligner/`，本目录仅负责输入转换、模型接口兼容与共享执行衔接。`method/skillaligner/prompt.txt` 转录附录 A.6 Figure 4 的适配模板。三个逻辑阶段在**同一次**模型调用完成，严格验证返回的 `Primary / Checks / Avoid / Fallback`，每项必须是字符串列表，允许空列表。指南在后续执行期间固定，源技能不更新。无效 JSON、截断或调用失败会报错，不重试、不用原技能代替。

输入必须显式提供当前任务 query 和固定 execution specification（真实工具、schema、动作格式、资源、权限和交互约束）。不要提供参考答案、评分器代码或执行后观察。当前入口不从各 benchmark 自动提取这些信息，调用方必须保证它们与执行配置一致。

## 运行

在 skillpatch 仓库根目录：

```bash
.venvs/methods/bin/python -m method_input_adapters.skillaligner \
  --manifest configs/skillaligner.example.json \
  --output runs/skillaligner/example --model <模型名> \
  --env-file <凭据文件> --adapt-only
```

凭据变量默认 `METHOD_API_KEY`、`METHOD_BASE_URL`，可以用 `--api-key-env`、`--base-url-env` 指定。不会自动执行示例中的合成任务。输出目录必须全新。

去掉 `--adapt-only` 即通过 `SharedExecutorBridge` 正式执行，manifest 每条还需加入项目现有的 `config`、`split` 和原生任务的 `task_ref` 字段，格式见上级 README。适配和执行使用各自配置的模型；公平比较应显式使两者一致并记录配置。执行预算和模型由原执行 TOML 控制。

SkillsBench 生成仅包含 execution-guide 的 pack，替换原始技能 pack；原生 ALFWorld、ScienceWorld、AppWorld 将指南作为文本 skill bank 交给现有 runner。适配指南不会附带原技能或脚本。若所需脚本本来作为环境资源可用，应在执行环境中独立提供并在 specification 中说明。后两种环境属于向 SkillPatch 的迁移，非原论文 benchmark。当前未新增 WebShop 或 SearchQA 环境。

每条输入支持二选一：

- `retrieved_skills`: 已检索候选，按传入顺序使用，方便与其他 baseline 固定完全相同的候选。
- `skill_library`: 完整规范化技能记录，使用 BM25 选择 top-5。

记录含 `id`、`name`、`text`，可选 `description`。全文放入请求，不做隐藏截断。BM25 参数 k1=1.5、b=0.75，Unicode 单词分词、并列按原库顺序；这些是论文未指定的工程选择。论文原技能库快照、规范化流程及完整环境 specification 未公开，此处要求提供相应输入，不能声称恢复了原实验数据。

适配采用 temperature=1、top_p=1；max_tokens 默认4096（工程选择，论文未报告该上限）。沿用项目 GPT-5.5 参数兼容逻辑：该模型使用服务默认采样参数。客户端不额外发送供应商专属 thinking 参数，需在模型服务端关闭 thinking（若支持）。

## 产物与验证

每任务保存 `adaptation/inputs.json`、`request.json`、`response.json`、`guide.json`、`result.json` 及 `guide.md`；正式执行额外保存共享执行器产物、分数和成功状态。原始响应包含服务返回的 token usage；缺失时记 null，不估算。这里只报告适配 token，完整推理成本需另加执行器记录的全部调用（包括修复调用）。

```bash
.venvs/methods/bin/python -m unittest tests.test_skillaligner -v
```

当前验证覆盖 BM25、输入替换、严格 schema、一次调用、token 存档和截断失败。测试使用 mock 模型，不证明任务收益。正式 baseline 比较还需在同一任务、模型、预算和候选下分别运行 no-skill、raw-skill、SkillAligner，再统计成功率、transfer、regression 与总 tokens。
