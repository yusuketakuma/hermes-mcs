"""専用の状態監視ジョブを短時間だけ実行します。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _mcs_path  # noqa: E402,F401
from cli_args import JapaneseArgumentParser  # noqa: E402
from sdc_cli import tick  # noqa: E402

if __name__ == '__main__':
    parser = JapaneseArgumentParser(description=__doc__)
    parser.add_argument('--state-root', type=Path)
    raise SystemExit(tick(parser.parse_args().state_root))
