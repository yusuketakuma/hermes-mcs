#!/usr/bin/env python3
"""v3 publisher中央API client (CEN09/PUB): OIDC認証の6 endpoint呼出。

request/responseはcontracts-v3 schemaで両側検査する。実networkは
transport注入で分離し、試験はfake transportのみ使う。認証はGitHub
Actions OIDC (`Authorization: Bearer <JWT>`)。redirectは追わない。
"""
import base64
import hashlib
import json
import os
import urllib.error
import urllib.request

from adapters.sdc import release_auto_contract as contract

API_BASE_ENV = 'MCS_PUBLISH_API_BASE'
CHUNK_BYTES = 16 * 1024 * 1024
MAX_CHUNKS = 16
MAX_OBJECT_BYTES = 256 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
PLAN_PURPOSE = 'mcs-publication-plan-v3'


class PublishApiError(RuntimeError):
    def __init__(self, code, status=None):
        super().__init__(code)
        self.code = code
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _opener():
    return urllib.request.build_opener(_NoRedirect)


def _read_bounded(response, limit=MAX_RESPONSE_BYTES):
    data = response.read(limit + 1)
    if len(data) > limit:
        raise PublishApiError('RESPONSE_TOO_LARGE')
    return data


def api_request(method, url, *, token, body=None, raw=None, headers=None,
                timeout_s=30, opener=None):
    """JSON/binary 1回のHTTP呼出。redirect不追従・bounded read・Bearer付与。"""
    if not isinstance(url, str) or not url.startswith('https://'):
        raise PublishApiError('URL_INVALID')
    hdrs = {'authorization': 'Bearer ' + token,
            'accept': 'application/json',
            'cache-control': 'no-store'}
    data = None
    if raw is not None:
        data = raw
        hdrs['content-type'] = 'application/octet-stream'
    elif body is not None:
        data = json.dumps(body, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':')).encode()
        hdrs['content-type'] = 'application/json'
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs,
                                 method=method)
    open_fn = opener or _opener().open
    try:
        with open_fn(req, timeout=timeout_s) as response:
            status = getattr(response, 'status', 200)
            payload = _read_bounded(response)
    except urllib.error.HTTPError as error:
        try:
            detail = _read_bounded(error, 65536)
        except Exception:
            detail = b''
        code = 'HTTP_%d' % error.code
        try:
            doc = json.loads(detail)
            if isinstance(doc, dict) and isinstance(doc.get('code'), str):
                code = doc['code']
        except Exception:
            pass
        raise PublishApiError(code, error.code) from None
    except PublishApiError:
        raise
    except Exception as error:
        raise PublishApiError('TRANSPORT_ERROR') from error
    if not 200 <= status < 300:
        raise PublishApiError('HTTP_%d' % status, status)
    return payload


def oidc_token(env=None, *, opener=None):
    """GitHub Actions OIDC JWTを取得する。値は返さずheader用tokenとしてのみ使う。"""
    env = os.environ if env is None else env
    url = env.get('ACTIONS_ID_TOKEN_REQUEST_URL')
    req_token = env.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN')
    if not url or not req_token:
        raise PublishApiError('OIDC_UNAVAILABLE')
    req = urllib.request.Request(
        url, data=b'', method='POST',
        headers={'authorization': 'Bearer ' + req_token,
                 'accept': 'application/json'})
    open_fn = opener or _opener().open
    try:
        with open_fn(req, timeout=15) as response:
            doc = json.loads(_read_bounded(response, 65536))
    except PublishApiError:
        raise
    except Exception as error:
        raise PublishApiError('OIDC_UNAVAILABLE') from error
    value = doc.get('value') if isinstance(doc, dict) else None
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise PublishApiError('OIDC_INVALID')
    return value


def api_base(env=None):
    env = os.environ if env is None else env
    base = env.get(API_BASE_ENV)
    if not isinstance(base, str) or not base.startswith('https://'):
        raise PublishApiError('API_BASE_UNCONFIGURED')
    return base.rstrip('/')


def _parse(definition, value):
    return contract.parse_document(
        json.dumps(value, ensure_ascii=False).encode(), definition,
        max_bytes=MAX_RESPONSE_BYTES)


def _post(base, path, token, body, definition, *, opener=None, timeout_s=30):
    raw = api_request('POST', base + path, token=token, body=body,
                      opener=opener, timeout_s=timeout_s)
    try:
        doc = json.loads(raw)
    except Exception:
        raise PublishApiError('RESPONSE_INVALID') from None
    return _parse(definition, doc)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def chunk_plan(data):
    """asset bytesを仕様のchunk列へ分割する(index順・16MiB・末尾のみ可変)。"""
    size = len(data)
    if size < 1 or size > MAX_OBJECT_BYTES:
        raise PublishApiError('ASSET_SIZE')
    chunks = []
    offset = 0
    index = 0
    while offset < size:
        part = data[offset:offset + CHUNK_BYTES]
        chunks.append({'index': index, 'sha256': sha256_bytes(part),
                       'size': len(part), 'data': part})
        offset += len(part)
        index += 1
    return chunks


def object_manifest_request(*, request_id, publication_id, expected_revision,
                            fencing_token, data, content_type):
    """asset bytesからObjectManifestRequest(contract)を組み立てる。"""
    chunks = chunk_plan(data)
    return {'schema_version': 'mcs.object-manifest-request.v3',
            'request_id': request_id,
            'publication_id': publication_id,
            'expected_revision': expected_revision,
            'fencing_token': fencing_token,
            'sha256': sha256_bytes(data),
            'size': len(data),
            'content_type': content_type,
            'chunks': [{k: c[k] for k in ('index', 'sha256', 'size')}
                       for c in chunks]}, chunks


def reserve(base, token, build_request, *, opener=None):
    return _post(base, '/v3/mcs/publications/reserve', token,
                 build_request, 'PublicationReservation', opener=opener)


def register_manifest(base, token, manifest_request, *, opener=None):
    return _post(base, '/v3/mcs/publications/object-manifests', token,
                 manifest_request, 'ObjectReceipt', opener=opener)


def put_chunk(base, token, *, publication_id, object_sha256, index,
              manifest_sha256, fencing_token, request_id, data,
              opener=None):
    """chunk bytesをPUTする。manifest_sha256は'object-manifest' hash。"""
    url = (base + '/v3/mcs/publications/objects/' + object_sha256
           + '/chunks/' + str(index))
    headers = {'x-mcs-object-manifest-sha256': manifest_sha256,
               'x-mcs-publication-id': publication_id,
               'x-mcs-fencing-token': str(fencing_token),
               'x-mcs-request-id': request_id}
    raw = api_request('PUT', url, token=token, raw=data, headers=headers,
                      opener=opener)
    try:
        doc = json.loads(raw)
    except Exception:
        raise PublishApiError('RESPONSE_INVALID') from None
    return _parse('ObjectReceipt', doc)


def verify_object(base, token, *, request_id, publication_id,
                  expected_revision, fencing_token, manifest_sha256,
                  opener=None):
    body = {'schema_version': 'mcs.object-verify-request.v3',
            'request_id': request_id,
            'publication_id': publication_id,
            'expected_revision': expected_revision,
            'fencing_token': fencing_token,
            'manifest_sha256': manifest_sha256}
    return _post(base, '/v3/mcs/publications/objects/verify', token,
                 body, 'ObjectReceipt', opener=opener)


def object_manifest_sha256(manifest_request):
    """中央artifact-store.jsと同じkindでmanifest hashを計算する。"""
    return contract.hash_v3('object-manifest', manifest_request)


def finalize_request(*, publication_id, expected_revision, fencing_token,
                     release_core_json, compatibility_payloads,
                     candidate_assets):
    """PublicationFinalizeRequest(contract)を組み立て検査する。"""
    doc = {'schema_version': 'mcs.publication-finalize-request.v3',
           'publication_id': publication_id,
           'expected_revision': expected_revision,
           'fencing_token': fencing_token,
           'release_core_json': release_core_json,
           'compatibility_payloads': compatibility_payloads,
           'candidate_assets': candidate_assets}
    return _parse('PublicationFinalizeRequest', doc)


def finalize(base, token, finalize_request_doc, *, opener=None):
    """SignedEnvelopeを返す。呼出側がpurpose/payload/署名を検証する。"""
    raw = api_request('POST', base + '/v3/mcs/publications/finalize',
                      token=token, body=finalize_request_doc, opener=opener)
    try:
        doc = json.loads(raw)
    except Exception:
        raise PublishApiError('RESPONSE_INVALID') from None
    if not isinstance(doc, dict):
        raise PublishApiError('RESPONSE_INVALID')
    return doc


def decode_plan(envelope):
    """SignedEnvelope.payload(base64url canonical JSON)をPublishPlanへ戻す。"""
    payload = envelope.get('payload') if isinstance(envelope, dict) else None
    if not isinstance(payload, str):
        raise PublishApiError('PLAN_INVALID')
    try:
        raw = base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4))
        doc = json.loads(raw)
    except Exception:
        raise PublishApiError('PLAN_INVALID') from None
    return _parse('PublishPlan', doc)


def register(base, token, register_request, *, opener=None):
    return _post(base, '/v3/mcs/publications/register', token,
                 register_request, 'PublicationRecord', opener=opener)
