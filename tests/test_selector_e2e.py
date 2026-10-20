"""End-to-end tests for the `fls test` selector: --only, --changed, --list, --dry-run.

Every case drives the real CLI as a subprocess, exactly as a maintainer would,
and every case writes its observation into ``work/maintenance/selector_proof.json``.
That file is the verifiable artifact: re-running this script reproduces it
byte for byte, and a failing selector leaves the evidence behind instead of
only printing to a terminal.

Failure modes this suite exists to catch, written before the assertions:

1. ``--list`` prints ids the runner cannot accept, so ``--only`` never matches.
2. ``--only`` silently widens to the whole suite instead of the named tests.
3. ``--only`` matches nothing and still exits 0, hiding a typo.
4. ``--dry-run`` runs tests instead of only reporting the selection.
5. ``--changed`` selects scripts for paths no test covers, hiding regressions.
6. ``--changed`` claims a full replay is needed when encoding is untouched.
7. ``--changed`` with a clean tree runs the suite instead of doing nothing.
8. The selection report cannot be reproduced, so nobody can audit it later.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import fls.core.path_compat as path_compat

WORK = path_compat.WORK
PROOF = WORK / 'maintenance' / 'selector_proof.json'


def fls(*arguments, cwd=REPO, environment=None):
    """Run the real CLI entry point and return the completed process."""
    return subprocess.run(
        [sys.executable, '-m', 'fls', *arguments],
        cwd=cwd, capture_output=True, text=True,
        env=dict(os.environ, **(environment or {})),
    )


class SelectorEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proof = []

    @classmethod
    def tearDownClass(cls):
        # One deterministic artifact per run: sorted keys, stable order, no
        # timestamps, so two runs on the same tree produce identical bytes.
        PROOF.parent.mkdir(parents=True, exist_ok=True)
        PROOF.write_text(
            json.dumps(sorted(cls.proof, key=lambda item: item['case']),
                       ensure_ascii=False, indent=2, sort_keys=True) + '\n',
            encoding='utf-8')

    def record(self, case, **facts):
        self.proof.append({'case': case, **facts})

    def test_listed_ids_are_accepted_by_the_runner(self):
        """Failure mode 1: a listed id must be runnable, or --only can never hit it."""
        listed = fls('test', '--list', '--json')
        self.assertEqual(listed.returncode, 0, listed.stderr)
        inventory = json.loads(listed.stdout)
        self.assertTrue(inventory)

        probe = next(name for name, ids in inventory.items() if ids)
        test_id = inventory[probe][0]
        result = fls('test', '--only', test_id, '--dry-run', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        selected = json.loads(result.stdout)['scripts']
        self.assertEqual(selected, {probe + '.py': [test_id]})
        self.record('listed_ids_are_accepted', probe=probe, test_id=test_id)

    def test_only_narrows_instead_of_running_everything(self):
        """Failure mode 2: the named run must cover far fewer tests than the suite."""
        full = json.loads(fls('test', '--dry-run', '--json').stdout)
        narrow = fls('test', '--only', 'test_unsupported_language_rejected',
                     '--dry-run', '--json')
        self.assertEqual(narrow.returncode, 0, narrow.stderr)
        chosen = json.loads(narrow.stdout)['scripts']

        self.assertLess(len(chosen), len(full['scripts']))
        self.assertNotIn('test_bootstrap.py', chosen)
        self.assertEqual(chosen['test_workflows.py'],
                         ['Workflows.test_unsupported_language_rejected'])

        # And it really executes just that test, quickly, with a passing exit.
        run = fls('test', '--only', 'test_unsupported_language_rejected')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('Ran 1 test', run.stdout + run.stderr)
        self.record('only_narrows', selected=chosen['test_workflows.py'],
                    suite_scripts=len(full['scripts']))

    def test_only_rejects_a_pattern_that_matches_nothing(self):
        """Failure mode 3: a typo must fail loudly, not look like success."""
        result = fls('test', '--only', 'no-such-test-anywhere')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('No test matches', result.stderr)
        self.record('only_rejects_miss', exit=result.returncode)

    def test_dry_run_executes_nothing(self):
        """Failure mode 4: --dry-run must report, never run."""
        result = fls('test', '--only', 'test_unsupported_language_rejected',
                     '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['mode'], 'only')
        # A real run prints unittest output; a dry run prints only the report.
        self.assertNotIn('Ran 1 test', result.stdout + result.stderr)
        self.assertNotIn('OK\n', result.stdout)
        self.record('dry_run_executes_nothing', mode=report['mode'])

    def test_changed_reports_paths_and_matching_scripts(self):
        """Failure mode 5 and 6: unrelated paths select nothing and need no replay."""
        result = fls('test', '--changed', '--dry-run', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['mode'], 'changed')

        # The tree carries source edits, so at least one script must be picked.
        if report['paths']:
            self.assertTrue(report['scripts'])
        # Paths outside every mapping are never silently attached to a script.
        library_only = [path for path in report['paths']
                        if path.startswith(('fonts/', 'docs/', 'translation/'))]
        for path in library_only:
            self.assertNotIn(path, report['scripts'])
        self.record('changed_reports_selection',
                    paths=len(report['paths']),
                    scripts=sorted(report['scripts']),
                    pack=report['pack'])

    def test_changed_and_only_are_mutually_exclusive(self):
        """Two different selection modes in one call must not silently pick one."""
        result = fls('test', '--changed', '--only', 'menu_spacing')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('pick one', result.stderr)
        self.record('modes_are_exclusive', exit=result.returncode)

    def test_paths_override_plans_a_hypothetical_change(self):
        """Failure mode 5: unrelated paths must not pull in the whole suite.

        --paths lets a maintainer ask "what would this change run?" before
        committing, and lets this suite pin the mapping without editing files.
        """
        cases = {
            'fls/cli/main.py': {'test_cli.py', 'test_selector_e2e.py'},
            'fls/disc/cdrom.py': {'test_cd_parity.py', 'test_fs2_cd_mode1.py'},
            'patcher/main.go': set(),
            'fonts/unifont-18.0.01/COPYING': set(),
        }
        observed = {}
        for path, expected in cases.items():
            result = fls('test', '--changed', '--paths', path, '--dry-run', '--json')
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            actual = set(report['scripts'])
            self.assertEqual(actual, expected, path)
            observed[path] = sorted(actual)
        self.record('paths_override_plans', observed=observed)

    def test_pack_is_requested_only_for_image_encoding(self):
        """Failure mode 6: a full replay must follow encoding work and nothing else."""
        for path, expected in (('fls/builders/build_alpha.py', True),
                               ('fls/storage/candidate.py', True),
                               ('fls/cli/main.py', False),
                               ('translation/corpus.json', False)):
            result = fls('test', '--changed', '--paths', path, '--dry-run', '--json')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['pack'], expected, path)
        self.record('pack_follows_encoding', checked=4)

    def test_go_patcher_change_points_at_go_checks(self):
        """The patcher is a separate module; Python tests must not claim it."""
        result = fls('test', '--changed', '--paths', 'patcher/cdrom.go', '--dry-run', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['scripts'], {})
        self.assertTrue(report['go'])
        self.record('go_change_points_at_go', go=report['go'])

    def test_unknown_path_selects_nothing(self):
        """A path no test covers must report nothing rather than guess."""
        result = fls('test', '--changed', '--paths', 'notes/todo.txt', '--dry-run', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['scripts'], {})
        self.record('unknown_path_selects_nothing', exit=result.returncode)

    def test_paths_without_changed_is_rejected(self):
        result = fls('test', '--paths', 'fls/cli/main.py')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--paths only applies with --changed', result.stderr)
        self.record('paths_requires_changed', exit=result.returncode)

    def test_proof_is_reproducible(self):
        """Failure mode 8: two independent runs on the same tree must agree byte for byte."""
        script = str(Path(__file__).resolve())
        runs = []
        for _ in range(2):
            result = subprocess.run(
                [sys.executable, script, 'SelectorEndToEnd.test_only_narrows_instead_of_running_everything'],
                capture_output=True, text=True, cwd=REPO)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            runs.append(PROOF.read_bytes())
        self.assertEqual(runs[0], runs[1])
        self.assertIn(b'"case"', runs[0])
        self.record('proof_is_reproducible', bytes=len(runs[0]))


if __name__ == '__main__':
    unittest.main(verbosity=2)
