import json
from pathlib import Path
import tempfile
import unittest

from scripts.skillsbench.preparation_compat import apply_compatibility, rewrite_dockerfile


class PreparationCompatibilityTests(unittest.TestCase):
    def specification(self):
        return {'packages': ['setuptools==81.0.0'], 'before_line': 'import seisbench.models as sbm'}

    def dockerfile(self):
        return ("FROM python:3.12-slim\nRUN pip install pandas==2.2.3 seisbench==0.10.2\n"
                "RUN python - <<'PY'\nimport seisbench.models as sbm\nsbm.PhaseNet.from_pretrained('instance')\nPY\n")

    def test_dependency_is_inserted_before_import_without_altering_pins_or_code(self):
        original = self.dockerfile()
        result = rewrite_dockerfile(original, self.specification())
        self.assertIn('RUN pip install pandas==2.2.3 seisbench==0.10.2\n', result)
        self.assertIn('RUN python -m pip install --no-cache-dir --no-deps setuptools==81.0.0', result)
        self.assertLess(result.index('setuptools==81.0.0'), result.index('import seisbench'))
        self.assertTrue(result.endswith(original[original.index("RUN python - <<'PY'"):]))
        self.assertEqual(rewrite_dockerfile(result, self.specification()), result)

    def test_only_the_approved_task_temporary_context_is_changed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            policy = {'tasks': {'earthquake-phase-association': self.specification()}}
            (root / 'PREPARATION_COMPATIBILITY_REQUESTED.json').write_text(json.dumps(policy))
            for name in ('earthquake-phase-association', 'other-task'):
                context = root / 'sdk' / name / 'environment'
                context.mkdir(parents=True)
                (context / 'Dockerfile').write_text(self.dockerfile())
                changed = apply_compatibility(context, root / 'audit' / name)
                self.assertEqual(changed, name == 'earthquake-phase-association')
                if changed:
                    self.assertEqual((root / 'audit' / name / 'Dockerfile.before').read_text(), self.dockerfile())
                    self.assertFalse(apply_compatibility(context, root / 'audit' / name))
                else:
                    self.assertEqual((context / 'Dockerfile').read_text(), self.dockerfile())

    def test_continued_install_and_model_import_remain_one_unmodified_run(self):
        original = ("FROM python:3.12-slim\nRUN pip install --no-cache-dir seisbench==0.10.2 && \\\n"
                    "    python - <<'PY'\nimport seisbench.models as sbm\n"
                    'sbm.PhaseNet.from_pretrained("original")\n'
                    'sbm.DeepDenoiser.from_pretrained("original")\nPY\n')
        result = rewrite_dockerfile(original, self.specification())
        self.assertTrue(result.endswith(original[original.index('RUN pip install'):]))
        self.assertLess(result.index('setuptools==81.0.0'), result.index('RUN pip install'))
        self.assertEqual(result.count('setuptools==81.0.0'), 1)
        self.assertEqual(rewrite_dockerfile(result, self.specification()), result)

    def test_missing_or_ambiguous_import_anchor_is_rejected(self):
        for text in ('FROM python:3.12-slim\n', self.dockerfile() + self.dockerfile()):
            with self.assertRaisesRegex(ValueError, 'missing or ambiguous'):
                rewrite_dockerfile(text, self.specification())

    def test_new_dependency_layer_inherits_existing_download_routing(self):
        from scripts.skillsbench.apt_mirrors import rewrite_dockerfile as route_downloads
        original = route_downloads(self.dockerfile())
        result = rewrite_dockerfile(original, self.specification())
        inserted = next(line for line in result.split('\n') if 'pip install --no-cache-dir --no-deps setuptools' in line)
        self.assertIn('PIP_INDEX_URL', inserted)
        self.assertTrue(result.endswith(original[original.index("RUN : SKILLPATCH_APT_MIRROR_V1", original.index('seisbench==0.10.2')):]))

    def test_dependency_versions_must_be_explicit(self):
        for package in ('setuptools', 'setuptools<82', 'setuptools==81.0.0;echo bad'):
            with self.assertRaisesRegex(ValueError, 'exact approved versions'):
                rewrite_dockerfile(self.dockerfile(), {**self.specification(), 'packages': [package]})
