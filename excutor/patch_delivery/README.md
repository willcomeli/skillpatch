# 可选补丁交付

只消费已生成的 `SKILL_PATCHES.md` + `SKILL_PATCHES.json`，验证格式、目标版本、引文及 Markdown 一致性。不生成补丁，也不实现补丁注册库。没有传补丁参数时沿用原执行路径，不修改原始 Skill 或 Skill bank。

## SkillsBench / BenchFlow

传入完整 Skill 包（目录的直接子目录各含一个 SKILL.md），保留所有原始文件和资源，只允许侧车补丁文件有所不同：

```bash
python -m set_up.cli prepare --config configs/example.toml --output runs/patch-preview --patch-skills-dir /path/to/full-patched-skills
python -m set_up.cli run --config configs/example.toml --output runs/patch-run --patch-skills-dir /path/to/full-patched-skills
```

也可以直接调用：

```python
result = executor.run(request, output, environment, patch_skills_dir=patched_pack)
```

必须使用 with-skill、开启 explicit_skill_read；具体 Agent 必须有受支持的提示增强器（当前为 OpenHands）。执行器核验实际复制出来的完整包摘要。部署记录为运行目录中的 patch_deployment.json，任务输入和提示保存在现有快照目录。未传该参数不选择补丁，也不移除调用方原本就提供的 Skill 包内的补丁。

## ALFWorld / ScienceWorld / AppWorld

三个 Runner 都支持在单任务及批量入口增加一个可选关键字参数：

```python
row = runner.run_task(task, original_skill_bank, patch_skill_dir=single_patched_skill)
rows = runner.run_task_set(tasks, original_skill_bank, patch_skill_dir=single_patched_skill)
```

single_patched_skill 是单个 Skill 目录，含原始 SKILL.md 和经核验的补丁文件对。原 Skill bank 仍负责检索，且 format_for_prompt() 必须返回与该 SKILL.md 完全相同的文本；混合多 Skill 或其他排版的检索结果会明确拒绝。补丁文本追加到该文本后交给动作模型；原 Skill bank 不修改。原生返回字典保留原字段，带补丁时增加 patch_deployment；retrieved_memory_text 记录实际交付的完整内容，诊断适配器可直接消费。

不带补丁：省略 patch_skill_dir，方法行为和结果字段保持原样。原生 Runner 的 checkpoint_path/checkpoint_dir 暂不支持与补丁参数同时使用，以免复用无补丁旧结果；应从初始任务重新运行。
