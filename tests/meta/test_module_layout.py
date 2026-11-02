"""Fresh imports use this checkout and retain clinical behavior in either import order."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULES = {
    'anthropic_client': 'llm/providers/anthropic_client.py',
    'openai_client': 'llm/providers/openai_client.py',
    'cloud_client': 'llm/transport/cloud_client.py',
    'local_llm': 'llm/transport/local_llm.py',
    'llm_retry': 'llm/transport/llm_retry.py',
    'llm_admission': 'llm/admission/llm_admission.py',
    'llm_availability': 'llm/admission/llm_availability.py',
    'extract': 'extract/rules/extract.py',
    'extract_llm': 'extract/llm/extract_llm.py',
    'clinical_values': 'extract/clinical/clinical_values.py',
    'clinical_chunking': 'extract/clinical/clinical_chunking.py',
    'semantic_quantities': 'extract/clinical/semantic_quantities.py',
    'patient_context': 'patients/patient_context.py',
    'patient_context_refresh': 'patients/patient_context_refresh.py',
    'patient_context_projection': 'patients/patient_context_projection.py',
    'extraction_context': 'extract/llm/extraction_context.py',
    'semantic_publication': 'semantic/semantic_publication.py',
    'semantic_conversion': 'semantic/semantic_conversion.py',
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
import sys, importlib, json, hashlib
from pathlib import Path
sys.path.insert(0, {str(ROOT / 'mcs')!r})
import _mcs_path
importlib.import_module({first!r})
for name, relative in {MODULES!r}.items():
    module = importlib.import_module(name)
    assert Path(module.__file__).resolve() == Path({str(ROOT / 'mcs')!r}) / relative, name
import extract_llm
for source_label, module_name in {{
    "semantic_v4": "semantic_publication",
    "semantic_v4_cohort": "semantic_conversion",
    "patient_state_context": "extraction_context",
    "patient_context_state": "patient_context_projection",
}}.items():
    module = importlib.import_module(module_name)
    assert extract_llm._LOADED_SOURCE_DIGESTS[source_label] == hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
    assert module_name not in extract_llm._LOADED_SOURCE_DIGESTS
for name in ("CloudFallback", "CloudBusy", "_CloudProvider", "_current_cloud_settings", "_cloud_settings",
             "_cloud_contract", "_known_cloud_contract", "_cloud_repair_answer_evidence"):
    assert not hasattr(extract_llm, name), name
import clinical_values
assert clinical_values.grounded_vitals('本人P72 家族80', {{'hr':72}}) == {{'hr':72}}
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
