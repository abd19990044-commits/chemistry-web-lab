from pathlib import Path
import runpy

# Apply the main hardening patch first, then correct production bootstrap/test
# details discovered by the first verification run.
runpy.run_path('.github/scripts/production_hardening_kaggle_native.py', run_name='__main__')

# The application uses SECRET_KEY, not FLASK_SECRET_KEY. A dedicated
# ORCA_CALLBACK_SECRET still takes precedence.
p = Path('orca_orchestrator/callback_auth.py')
text = p.read_text(encoding='utf-8')
text = text.replace('_FALLBACK_ENV = "FLASK_SECRET_KEY"', '_FALLBACK_ENV = "SECRET_KEY"')
p.write_text(text, encoding='utf-8')

p = Path('.env.example')
text = p.read_text(encoding='utf-8')
text = text.replace('FLASK_SECRET_KEY=change_this_to_a_long_random_secret',
                    'SECRET_KEY=change_this_to_a_long_random_secret')
p.write_text(text, encoding='utf-8')

# Correct test construction: JobStore accepts StoreConfig, not a DB pathname.
p = Path('tests/test_kaggle_native_production.py')
text = p.read_text(encoding='utf-8')
text = text.replace('from orca_orchestrator.credentials import KaggleCredentials\n', '')
text = text.replace('from orca_orchestrator.models import JobManifest\n', '')
text = text.replace('from orca_orchestrator.service import OrchestratorService\n',
                    'from orca_orchestrator.service import OrchestratorService\nfrom orca_orchestrator.config import StoreConfig\n')
text = text.replace("JobStore(str(tmp_path / 'state.db'))",
                    "JobStore(StoreConfig(state_dir=str(tmp_path), db_filename='state.db'))")
p.write_text(text, encoding='utf-8')

# A Popen/exec failure is not resumable work. Mark it explicitly and teach the
# shared outcome classifier to fail the job instead of creating restart windows.
p = Path('orca_orchestrator/runner/kernel_runner.py')
text = p.read_text(encoding='utf-8')
old = '''        except OSError as exc:\n            out_fh.close()\n            fail_log("starting ORCA", str(exc),\n                     "none: the executable could not be launched at all",\n                     "failing the job; without a runnable ORCA binary nothing can proceed")\n            return None\n'''
new = '''        except OSError as exc:\n            out_fh.close()\n            self.stop_reason = "launch"\n            self.stop_detail = str(exc)\n            fail_log("starting ORCA", str(exc),\n                     "none: the executable could not be launched at all",\n                     "failing the job; without a runnable ORCA binary nothing can proceed")\n            return None\n'''
assert old in text
p.write_text(text.replace(old, new, 1), encoding='utf-8')

p = Path('orca_orchestrator/orca_artifacts.py')
text = p.read_text(encoding='utf-8')
needle = '''    if killed_by == "time":\n        return make(OUTCOME_INCOMPLETE, "the run was stopped by the session-time watchdog")\n\n    # 2. Disk exhaustion reported by ORCA or the OS.\n'''
replacement = '''    if killed_by == "time":\n        return make(OUTCOME_INCOMPLETE, "the run was stopped by the session-time watchdog")\n    if killed_by == "launch":\n        return make(OUTCOME_FATAL,\n                    "the ORCA executable could not be launched; restarting the same kernel "\n                    "cannot repair a missing or non-executable binary")\n\n    # 2. Disk exhaustion reported by ORCA or the OS.\n'''
assert needle in text
p.write_text(text.replace(needle, replacement, 1), encoding='utf-8')

# Extend the regression file with launch-failure classification.
p = Path('tests/test_kaggle_native_production.py')
text = p.read_text(encoding='utf-8')
text += '''\n\ndef test_launch_failure_is_fatal_not_restartable():\n    from orca_orchestrator import orca_artifacts as art\n    outcome = art.classify_outcome('', job_kind='opt', killed_by='launch')\n    assert outcome.is_fatal\n    assert not outcome.is_continuable\n'''
p.write_text(text, encoding='utf-8')

print('Production hardening fix2 applied')
