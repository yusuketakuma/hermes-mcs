#!/usr/bin/env python3
"""ci.yml tag eventのpublication-request hintを生成する(W12)。

値はrepo ID/tag/tag OID/source SHA/upstream run ID/attemptのみで4KiB未満。
このhintは未信頼データであり、権威ある照合(tag peel・repo/owner ID・
acceptance)はrelease-publish workflowのidentify jobが行う。
"""
import json
import os
import sys


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else 'publication-request.json'

    def _opt(name):
        return os.environ.get(name) or None

    doc = {
        'schema_version': 'mcs.publication-request-hint.v1',
        'repository_id': int(os.environ['REPOSITORY_ID']),
        'tag': os.environ['REF_NAME'],
        'tag_oid': os.environ['SHA'],
        'source_sha': _opt('HEAD_COMMIT_ID'),
        'upstream_run_id': int(os.environ['RUN_ID']),
        'upstream_run_attempt': int(os.environ['RUN_ATTEMPT']),
    }
    raw = json.dumps(doc, separators=(',', ':')).encode()
    if len(raw) > 4096:
        sys.exit('publication-request hint exceeds 4KiB')
    with open(out, 'wb') as f:
        f.write(raw + b'\n')


if __name__ == '__main__':
    main()
