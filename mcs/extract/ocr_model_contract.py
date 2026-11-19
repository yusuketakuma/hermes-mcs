"""Pin the OCR engine and model revisions used by installation and inference."""
import hashlib
import json
from pathlib import Path

ENGINE_VERSION = "0.16.3"
MODELS = {
    "detector": ("KotaroKinoshita/yomitoku-text-detector-dbnet-v2_1",
                 "3ac8375d1fd124e8074b013a32f2085d1199654c"),
    "recognizer": ("KotaroKinoshita/yomitoku-text-recognizer-parseq-middle-dynw-v5",
                   "cfe941a992ba3eedbdd3d54ef22ef34d39a88fa7"),
    "layout": ("KotaroKinoshita/yomitoku-layout-parser-rtdtrv2-v2",
               "6772f1b6ea5b694da2e8fe19cf6d38f453822616"),
    "table": ("KotaroKinoshita/yomitoku-table-structure-recognizer-rtdtrv2-open-beta",
              "91103cbcefc7f9d0ba8234fd3caba28b6d564a46"),
}
MODEL_JSON = json.dumps(MODELS, sort_keys=True, separators=(",", ":"))
MODEL_DIGEST = hashlib.sha256(MODEL_JSON.encode()).hexdigest()
RUNTIME_CONTRACT = f"yomitoku-{ENGINE_VERSION}-cpu-text-first/table-grid-v2/{MODEL_DIGEST}"


def hf_home(data_dir):
    """HF cache kept inside the MCS data directory so cache cleaners cannot drop it."""
    return Path(data_dir) / "huggingface"
