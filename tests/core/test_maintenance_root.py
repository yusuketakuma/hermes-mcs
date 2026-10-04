"""maintenance/local_llm paths follow MCS_ROOT (standalone --root)."""
import os
import subprocess
import sys

import local_llm
import maintenance


def test_maintenance_and_admission_paths_follow_mcs_root(tmp_path):
    code = (
        "import maintenance, local_llm, os\n"
        "os.environ['MCS_LLM_ADMISSION'] = '1'\n"
        "print(maintenance.HOME); print(maintenance.BACKUP_DIR)\n"
        "print(maintenance.SNAPSHOT_DIR); print(maintenance.LOGFILE)\n"
        "print(local_llm._admission_db_path())\n")
    env = dict(os.environ, MCS_ROOT=str(tmp_path),
               PYTHONPATH=os.pathsep.join(
                   {os.path.dirname(maintenance.__file__),
                    os.path.dirname(local_llm.__file__)}))
    out = subprocess.run([sys.executable, "-c", code], env=env, check=True,
                         capture_output=True, text=True).stdout.split("\n")
    lines = [line for line in out if line]
    assert len(lines) == 5, out
    root = os.path.realpath(tmp_path)
    for line in lines:
        assert os.path.realpath(line).startswith(root), line
