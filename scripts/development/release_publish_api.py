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
import re
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


# --- GitHub Releases API (spec 01 §6) ---------------------------------------

GITHUB_API = 'https://api.github.com'
GITHUB_UPLOAD = 'https://uploads.github.com'
_GITHUB_HOSTS = frozenset({'api.github.com', 'uploads.github.com'})
MAX_GITHUB_PAGES = 20


def _github_url_ok(url):
    from urllib.parse import urlparse
    p = urlparse(url)
    return (p.scheme == 'https' and p.hostname in _GITHUB_HOSTS
            and not p.username and not p.password and not p.fragment)


def github_request(method, url, *, token, body=None, raw=None,
                   headers=None, opener=None):
    """api.github.com/uploads.github.com 限定のGitHub API呼出。"""
    if not _github_url_ok(url):
        raise PublishApiError('GITHUB_URL_INVALID')
    hdrs = {'authorization': 'Bearer ' + token,
            'accept': 'application/vnd.github+json',
            'x-github-api-version': '2022-11-28',
            'cache-control': 'no-store',
            'user-agent': 'mcs-release-publish'}
    data = None
    if raw is not None:
        data = raw
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
        with open_fn(req, timeout=30) as response:
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
            if isinstance(doc, dict) and isinstance(doc.get('message'), str):
                code = doc['message'][:80]
        except Exception:
            pass
        raise PublishApiError(code, error.code) from None
    except PublishApiError:
        raise
    except Exception as error:
        raise PublishApiError('TRANSPORT_ERROR') from error
    try:
        doc = json.loads(payload) if payload else None
    except Exception:
        doc = None
    return status, doc


def github_repo_slug(env=None):
    env = os.environ if env is None else env
    repo = env.get('MCS_PUBLISH_GITHUB_REPO')
    if not isinstance(repo, str) or re.fullmatch(
            r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) is None:
        raise PublishApiError('GITHUB_REPO_UNCONFIGURED')
    return repo


def github_get_tag_ref(repo, tag, token, *, opener=None):
    """GET /git/ref/tags/{tag} → object{sha,type}。404はNone。"""
    status, doc = _github_call(
        'GET', GITHUB_API + '/repos/' + repo + '/git/ref/tags/' + tag,
        token, opener=opener, not_found_none=True)
    if doc is None:
        return None
    obj = doc.get('object') if isinstance(doc, dict) else None
    if not isinstance(obj, dict) or not isinstance(obj.get('sha'), str):
        raise PublishApiError('GITHUB_RESPONSE_INVALID')
    return {'sha': obj['sha'], 'type': obj.get('type')}


def github_get_tag(repo, sha, token, *, opener=None):
    """annotated tag objectのpeeled commitを返す。"""
    status, doc = _github_call(
        'GET', GITHUB_API + '/repos/' + repo + '/git/tags/' + sha,
        token, opener=opener)
    obj = doc.get('object') if isinstance(doc, dict) else None
    if not isinstance(obj, dict) or obj.get('type') != 'commit' \
            or not isinstance(obj.get('sha'), str):
        raise PublishApiError('GITHUB_RESPONSE_INVALID')
    return obj['sha']


def github_find_release_by_tag(repo, tag, token, *, opener=None):
    """GET /releases/tags/{tag}。404はNone、draftも返す。"""
    status, doc = _github_call(
        'GET', GITHUB_API + '/repos/' + repo + '/releases/tags/' + tag,
        token, opener=opener, not_found_none=True)
    return doc if isinstance(doc, dict) else None


def github_create_draft(repo, *, tag_name, target_commitish, token,
                        opener=None):
    """POST /releases draft=true固定。返り値はrelease dict。"""
    status, doc = _github_call(
        'POST', GITHUB_API + '/repos/' + repo + '/releases', token,
        body={'tag_name': tag_name, 'target_commitish': target_commitish,
              'draft': True, 'prerelease': False, 'make_latest': False},
        opener=opener)
    if not isinstance(doc, dict) or not isinstance(doc.get('id'), int):
        raise PublishApiError('GITHUB_RESPONSE_INVALID')
    return doc


def github_list_assets(repo, release_id, token, *, opener=None):
    """GET /releases/{id}/assets 全page(最大20page・100/page)。"""
    out = []
    for page in range(1, MAX_GITHUB_PAGES + 1):
        status, doc = _github_call(
            'GET', GITHUB_API + '/repos/' + repo + '/releases/'
            + str(release_id) + '/assets?per_page=100&page=' + str(page),
            token, opener=opener)
        if not isinstance(doc, list):
            raise PublishApiError('GITHUB_RESPONSE_INVALID')
        out.extend(doc)
        if len(doc) < 100:
            return out
    raise PublishApiError('GITHUB_PAGINATION_LIMIT')


def github_upload_asset(release, name, data, token, *, opener=None):
    """draft releaseのupload_urlへassetをPUT相当POSTでuploadする。

    upload_url templateは `.../releases/{id}/assets{?name,label}`。
    hostはuploads.github.com固定・release id一致を検証してから使う。
    """
    template = release.get('upload_url') if isinstance(release, dict) else None
    if not isinstance(template, str):
        raise PublishApiError('GITHUB_RESPONSE_INVALID')
    from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
    parsed = urlparse(template)
    if parsed.scheme != 'https' or parsed.hostname != 'uploads.github.com' \
            or parsed.username or parsed.password or parsed.fragment:
        raise PublishApiError('GITHUB_UPLOAD_URL_INVALID')
    if ('/releases/' + str(release.get('id')) + '/assets') not in parsed.path:
        raise PublishApiError('GITHUB_UPLOAD_URL_INVALID')
    query = dict(parse_qsl(parsed.query))
    url = urlunparse(parsed._replace(
        query=urlencode({'name': name, **({'label': query['label']}
                                          if 'label' in query else {})})))
    status, doc = _github_call(
        'POST', url, token, raw=data,
        headers={'content-type': 'application/octet-stream',
                 'content-length': str(len(data))},
        opener=opener, expect=(201,))
    if not isinstance(doc, dict) or doc.get('state') != 'uploaded' \
            or doc.get('name') != name:
        raise PublishApiError('GITHUB_UPLOAD_INVALID')
    return doc


def github_publish_release(repo, release_id, token, *, opener=None):
    """PATCH draft=false, prerelease=false, make_latest=falseで公開。"""
    status, doc = _github_call(
        'PATCH', GITHUB_API + '/repos/' + repo + '/releases/'
        + str(release_id), token,
        body={'draft': False, 'prerelease': False, 'make_latest': False},
        opener=opener)
    if not isinstance(doc, dict) or doc.get('id') != release_id:
        raise PublishApiError('GITHUB_RESPONSE_INVALID')
    return doc


def github_get_release(repo, release_id, token, *, opener=None):
    status, doc = _github_call(
        'GET', GITHUB_API + '/repos/' + repo + '/releases/' + str(release_id),
        token, opener=opener)
    if not isinstance(doc, dict):
        raise PublishApiError('GITHUB_RESPONSE_INVALID')
    return doc


def _github_call(method, url, token, *, body=None, raw=None, headers=None,
                 opener=None, not_found_none=False, expect=None):
    """github_requestのstatus判定wrapper。404→None, expect集合外→error。"""
    try:
        status, doc = github_request(
            method, url, token=token, body=body, raw=raw, headers=headers,
            opener=opener)
    except PublishApiError as error:
        if not_found_none and error.status == 404:
            return 404, None
        raise
    if expect is not None and status not in expect:
        raise PublishApiError('GITHUB_STATUS_%d' % status, status)
    return status, doc


def register_request(*, publication_id, expected_revision, fencing_token,
                     verification):
    """PublicationRegisterRequest(contract)を組み立て検査する。"""
    doc = {'schema_version': 'mcs.publication-register-request.v3',
           'publication_id': publication_id,
           'expected_revision': expected_revision,
           'fencing_token': fencing_token,
           'verification': verification}
    return _parse('PublicationRegisterRequest', doc)
