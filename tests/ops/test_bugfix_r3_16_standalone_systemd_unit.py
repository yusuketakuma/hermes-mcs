"""Linux standalone unit: WorkingDirectory unquoted, '$' escaped in ExecStart."""
from pathlib import Path

from mcs_standalone import service


def test_linux_unit_working_directory_unquoted_and_dollar_escaped(tmp_path):
    root = tmp_path / "a$b"
    root.mkdir()
    _, body = service.render(root, platform="linux")
    repo = str(Path(service.__file__).resolve().parents[1])
    assert "WorkingDirectory=" + repo.replace("%", "%%") + "/.\n" in body
    exec_line = next(line for line in body.splitlines() if line.startswith("ExecStart="))
    assert "a$$b" in exec_line and "a$b" not in exec_line.replace("$$", "")
