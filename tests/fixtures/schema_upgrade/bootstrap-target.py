"""A synthetic target entrypoint, never a historical updater implementation."""
import json
import os
import sys

print(json.dumps({"entry": "synthetic-target", "argv": sys.argv[1:],
                  "repo": os.environ["MCS_UPDATE_REPO"], "cwd": os.getcwd(),
                  "file": __file__}))
