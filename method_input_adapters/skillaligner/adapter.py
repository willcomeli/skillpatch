"""Input contract and guide delivery for the shared benchmark executors."""
from pathlib import Path

from method.skillaligner import messages_for, retrieve, validate_skills, render_guide
from set_up.native_config import read_benchmark


def build_method_input(entry):
    """Use explicit public task/interface inputs; never read verifier feedback."""
    if ('retrieved_skills' in entry) == ('skill_library' in entry):
        raise ValueError('Specify exactly one of retrieved_skills or skill_library')
    skills = validate_skills(entry.get('retrieved_skills', entry.get('skill_library')))
    if 'skill_library' in entry:
        skills = retrieve(entry['query'], skills)
    query, specification = entry['query'], entry['execution_specification']
    messages_for(query, specification, skills)
    return query, specification, skills


def execute_guide(entry, guide, output, bridge):
    """Deliver only the adapted guide using the executor's native skill format."""
    output = Path(output)
    text = render_guide(guide)
    if read_benchmark(Path(entry['config'])) == 'skillsbench':
        pack = output.parent / 'guide_pack'
        (pack / 'execution-guide').mkdir(parents=True, exist_ok=False)
        (pack / 'execution-guide/SKILL.md').write_text(
            '---\nname: execution-guide\ndescription: Task-specific execution guidance\n---\n' + text,
            encoding='utf-8')
        return bridge.run(entry, '', output, skill_pack=pack)
    return bridge.run(entry, text, output)
