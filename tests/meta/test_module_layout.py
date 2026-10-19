"""Fresh imports use this checkout and retain clinical behavior in either import order."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULES = {
    'local_llm': 'llm/local_llm.py',
    'llm_admission': 'llm/llm_admission.py',
    'extract': 'extract/rules/extract.py',
    'extract_llm': 'extract/llm/extract_llm.py',
    'clinical_values': 'extract/clinical/clinical_values.py',
    'clinical_chunking': 'extract/clinical/clinical_chunking.py',
    'semantic_quantities': 'extract/clinical/semantic_quantities.py',
    'patient_context': 'patients/patient_context.py',
    'patient_nightly': 'patients/patient_nightly.py',
    'rollup': 'patients/rollup.py',
    'drug_map': 'medications/drug_map.py',
    'drug_alias': 'medications/drug_alias.py',
    'c1_contract': 'export/c1_contract.py',
    'c1_receiver': 'export/c1_receiver.py',
    'mcs_signals': 'signals/mcs_signals.py',
    'present_slack': 'notify/presentation/present_slack.py',
    'present_discord': 'notify/presentation/present_discord.py',
    'present_lineworks': 'notify/presentation/present_lineworks.py',
}


@pytest.mark.parametrize('first', ['extract', 'clinical_values'])
def test_fresh_imports_and_lazy_clinical_calls_use_the_current_layout(tmp_path, first):
    code = f'''
import sys, importlib, json
from pathlib import Path
sys.path.insert(0, {str(ROOT / 'mcs')!r})
import _mcs_path
importlib.import_module({first!r})
for name, relative in {MODULES!r}.items():
    module = importlib.import_module(name)
    assert Path(module.__file__).resolve() == Path({str(ROOT / 'mcs')!r}) / relative, name
import clinical_values
assert clinical_values._vitals_guard('本人P72 家族80', {{'hr':72}}) == {{'hr':72}}
assert clinical_values.patient_current_vitals({{'hr':80}}, '本日ご家族P80') == {{}}
assert clinical_values.patient_current_vitals({{'spo2':2}}, 'SpO2 95% 酸素2L') == {{}}
print(json.dumps({{'ok':True}}))
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=tmp_path,
                            env={**os.environ, 'MCS_ROOT': str(tmp_path / 'data-root')},
                            capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(result.stdout) == {'ok': True}


@pytest.mark.parametrize("isolated", [False, True], ids=["normal", "isolated"])
def test_snapshot_cli_bootstraps_before_extraction_imports_from_other_cwd(tmp_path, isolated):
    args = [sys.executable, "-B", *(["-I"] if isolated else []),
            str(ROOT / "mcs/semantic/eval/semantic_snapshot.py"), "--help"]
    result = subprocess.run(args, cwd=tmp_path,
                            env={**os.environ, "HOME": str(tmp_path / "home"),
                                 "MCS_ROOT": str(tmp_path / "data-root")},
                            capture_output=True, text=True, timeout=30, check=True)
    assert "blind-prepare" in result.stdout and "generate" in result.stdout
    assert "ModuleNotFoundError" not in result.stderr
    assert not (tmp_path / "data-root").exists()
