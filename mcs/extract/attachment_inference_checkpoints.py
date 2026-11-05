"""Resume source-bound local extraction leaves without owning writer or provider lifetimes."""
from __future__ import annotations

import copy
import json

import attachment_ocr
import attachment_feedback
import extract_llm
import extract_validation
import local_llm
from mcs_util import payload_hash

KIND = 'attachment_inference_checkpoint_v1'
SCHEMA = 'attachment-local-checkpoint/v1'


def validated_leaf(value, source, piece):
    """Validate retained local candidates and preserve pending quality as pending."""
    proof = extract_llm._leaf_evidence(value)
    if proof != {'providers': ['local'], 'complete': True}:
        return None
    candidate = {k: v for k, v in value.items() if k not in ('_quality_status', '_quality_result')}
    cleaned = extract_validation.validate(copy.deepcopy(candidate), source['body_text'])
    if cleaned is not None:
        for field in ('_items_dropped', '_evidence_dropped'):
            if type(candidate.get(field)) is int and candidate[field] >= 0:
                cleaned[field] = max(cleaned.get(field, 0), candidate[field])
    if (not extract_llm._response_has_required_fields(candidate) or cleaned is None
            or payload_hash(cleaned) != payload_hash(candidate)):
        return None
    state = value.get('_quality_status')
    quality = value.get('_quality_result')
    if state not in (None, 'pending', 'started'):
        return None
    if state is None:
        hints = extract_llm._rule_hints({'body_text': piece, 'posted_at': None})
        expected = extract_llm._hint_coverage(candidate, hints, piece)
        if not isinstance(quality, dict) or not extract_llm._quality_record_consistent(quality, expected):
            return None
    leaf = extract_llm._EvidenceLeaf(candidate, proof)
    if state is not None or quality.get('state') != 'hint_gaps_resolved':
        # A local interrupted repair can be retried; it has no paid-send uncertainty.
        leaf['_quality_status'] = 'pending'
    else:
        leaf['_quality_result'] = quality
    return leaf


def local_checkpoints(ledger, source, cfg, *, contract, write):
    """Bind resumable local leaves and split paths to one exact OCR occurrence."""
    size = extract_llm._CHUNK_SIZE
    identity = {'schema': SCHEMA, 'source_key': source['source_key'],
        'source_binding': source['_source_binding'], 'contract': contract(cfg),
        'plan': extract_llm.PLAN_VERSION, 'chunk_size': size}
    records = {}
    for row in ledger.db.execute("SELECT CASE WHEN typeof(content)='text' AND length(content)<=4194304 "
            "THEN content END FROM artifacts WHERE kind=? AND project_id=? AND artifact_id>? "
            "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.source_key') END=? "
            "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.binding') END=? "
            "ORDER BY artifact_id", (KIND, source['project_id'],
                                     attachment_feedback.revision(ledger.db, source['project_id'],
                                                                  source['source_key'], source['_source_binding']),
                                     source['source_key'],
                                     payload_hash(identity))):
        raw = extract_llm._provider_strict_dict(row[0])
        if not isinstance(raw, dict) or any(raw.get(k) != v for k, v in identity.items()):
            records.clear()
            continue  # corrupt history cannot resurrect an earlier leaf
        key = raw.get('key')
        if not extract_llm._checkpoint_keys(key):
            records.clear()
            continue
        records[key] = raw
    splits, chunks = {}, {}
    for key in sorted(records, key=lambda k: (str(k).count(':'), str(k))):
        raw = records[key]
        piece = extract_llm._chunk_piece(source['body_text'], size, key, splits)
        if piece is None or raw.get('piece_hash') != attachment_ocr._hash(piece):
            continue
        if raw.get('kind') == 'split':
            smaller = raw.get('size')
            if type(smaller) is int and 1 <= smaller < len(piece):
                parts = extract_llm.plan_chunks(piece, smaller)
                if len(parts) >= 2 and ''.join(parts) == piece:
                    splits[key] = smaller
        elif raw.get('kind') == 'leaf' and isinstance(raw.get('value'), dict):
            if raw.get('value_hash') != payload_hash(raw['value']):
                continue
            leaf = validated_leaf(extract_llm._EvidenceLeaf(raw['value'], raw.get('evidence')), source, piece)
            if leaf is not None:
                chunks[key] = leaf

    def store(key, value=None, smaller=None):
        piece = extract_llm._chunk_piece(source['body_text'], size, key, splits)
        if piece is None:
            return False
        raw = {**identity, 'key': key, 'piece_hash': attachment_ocr._hash(piece)}
        if smaller is not None:
            if type(smaller) is not int or not 1 <= smaller < len(piece):
                return False
            raw.update(kind='split', size=smaller)
        else:
            leaf = validated_leaf(value, source, piece)
            if leaf is None:
                return False
            raw.update(kind='leaf', value=dict(leaf), evidence=leaf.provider_evidence,
                       value_hash=payload_hash(dict(leaf)))
        def save():
            with ledger.db:
                ledger.artifact_add_tx(KIND, json.dumps(raw, ensure_ascii=False),
                    project_id=source['project_id'], message_id=source['parent_message_id'],
                    model=local_llm.resolve(cfg)[1],
                    meta={'source_key': source['source_key'], 'binding': payload_hash(identity)})
            return True
        if not write(save):
            return False
        if smaller is not None:
            splits[key] = smaller
            chunks.pop(key, None)
        else:
            chunks[key] = leaf
        return True
    return {'chunk_size': size, 'chunks_in': chunks, 'splits_in': splits,
            'on_chunk': lambda key, value: store(key, value),
            'on_split': lambda key, smaller: store(key, smaller=int(smaller)),
            'on_chunk_replay': lambda *args: True}
