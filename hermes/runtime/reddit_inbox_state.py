#!/usr/bin/env python3
"""Validate and persist Reddit inbox evidence without reading Reddit or posting."""
import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import urllib.parse
from datetime import datetime
from pathlib import Path

ALLOWED_SOURCES = {'old-reddit-messages', 'new-reddit-notifications', 'reddit-chat'}
ALLOWED_ACTIONS = {'posted_reply', 'already_replied', 'no_reply', 'skipped_insufficient_info'}
ARRAYS = ('actionable', 'noise', 'handled', 'skippedInsufficientInfo', 'postedReplies', 'deferred')
ARRAY_ACTIONS = {
    'actionable': {'actionable'},
    'noise': {'no_reply'},
    'handled': {'already_replied'},
    'skippedInsufficientInfo': {'skipped_insufficient_info'},
    'postedReplies': {'posted_reply'},
    'deferred': {'deferred', 'skipped_insufficient_info'},
}


def _storage_paths():
    value = os.getenv('REDDIT_MONITOR_DIR', '').strip()
    if not value:
        raise ValueError('REDDIT_MONITOR_DIR is required')
    base = Path(value)
    return base / 'state.json', base / 'inbox-cache' / 'latest.json', base / 'inbox-cache' / 'deferred.json'


def expected_reddit_user():
    value = os.getenv('REDDIT_EXPECTED_USER', '').strip()
    if not value:
        raise ValueError('REDDIT_EXPECTED_USER is required to validate posted reply evidence')
    return value


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def atomic_write_verified(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        with tmp.open('w', encoding='utf-8') as handle:
            json.dump(obj, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        back = read_json(path)
        if back != obj:
            raise RuntimeError(f'read-back mismatch: {path}')
        return back
    finally:
        if tmp.exists():
            tmp.unlink()


def norm_permalink(url):
    if not isinstance(url, str) or not url.strip():
        return ''
    original = url.strip()
    parsed = urllib.parse.urlparse(original)
    if parsed.scheme and parsed.scheme not in ('http', 'https'):
        return original
    path = re.sub(r'/+', '/', parsed.path).rstrip('/') + '/'
    if not path.startswith('/r/') and not path.startswith('/user/') and not path.startswith('/comments/'):
        return original
    return 'https://old.reddit.com' + path


def strict_permalink(url):
    value = norm_permalink(url)
    return value if isinstance(url, str) and value.startswith('https://old.reddit.com/') else ''


def aware(value, label):
    if not isinstance(value, str):
        raise ValueError(f'{label} must be an ISO-8601 string')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError(f'{label} is invalid') from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f'{label} must include a timezone')


def item_key(item):
    if not isinstance(item, dict):
        return ''
    return str(item.get('parentCommentId') or item.get('commentId') or item.get('messageId') or item.get('chatMessageId') or item.get('permalink') or item.get('commentPermalink') or '')


def validate_sources(sources):
    if not isinstance(sources, dict) or set(sources) != ALLOWED_SOURCES:
        raise ValueError('sources must cover exactly old-reddit-messages, new-reddit-notifications, reddit-chat')
    for source, evidence in sources.items():
        if not isinstance(evidence, dict) or evidence.get('status') not in {'verified', 'unverified', 'failed'}:
            raise ValueError(f'{source}: status must be verified, unverified, or failed')
        aware(evidence.get('checkedAt'), f'{source}.checkedAt')
        count = evidence.get('count')
        if evidence['status'] == 'verified':
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError(f'{source}.count must be a nonnegative integer when verified')
        elif count is not None:
            raise ValueError(f'{source}.count must be null unless verified')
        if not isinstance(evidence.get('coverage'), str) or not evidence['coverage'].strip():
            raise ValueError(f'{source}.coverage must be a nonempty string')


def validate_posted(item):
    if not isinstance(item, dict):
        raise ValueError('postedReplies entries must be objects')
    required = ('source', 'from', 'action', 'author', 'commentId', 'parentCommentId', 'commentPermalink', 'replyText', 'verification', 'composerReadback', 'postReloadReadback', 'handledAt')
    missing = [key for key in required if not item.get(key)]
    if missing:
        raise ValueError(f'posted reply missing fields: {missing}')
    if item.get('source') not in ALLOWED_SOURCES or item.get('action') != 'posted_reply':
        raise ValueError('posted reply source/action is invalid')
    expected_author = expected_reddit_user()
    if not isinstance(item['author'], str) or item['author'].casefold() != expected_author.casefold():
        raise ValueError('posted reply author does not match REDDIT_EXPECTED_USER')
    if not re.fullmatch(r't1_[A-Za-z0-9]+', str(item['commentId'])):
        raise ValueError('commentId must be a Reddit t1 ID')
    if not re.fullmatch(r't1_[A-Za-z0-9]+', str(item['parentCommentId'])):
        raise ValueError('parentCommentId must be a Reddit t1 ID')
    raw_permalink = item['commentPermalink']
    parsed = urllib.parse.urlparse(raw_permalink)
    if parsed.scheme not in ('http', 'https') or parsed.hostname not in {'old.reddit.com', 'www.reddit.com', 'reddit.com'}:
        raise ValueError('commentPermalink must use a Reddit hostname')
    if not parsed.path.startswith('/r/') and not parsed.path.startswith('/comments/'):
        raise ValueError('commentPermalink must be a Reddit comment permalink')
    if parsed.path.rstrip('/').split('/')[-1] != str(item['commentId'])[3:]:
        raise ValueError('commentPermalink final segment must match commentId suffix')
    verification = item['verification']
    if not isinstance(verification, dict) or any(verification.get(k) is not True for k in ('author', 'text', 'parent', 'comment')):
        raise ValueError('verification.author/text/parent/comment must all be true')
    if item['composerReadback'] != item['replyText'] or item['postReloadReadback'] != item['replyText']:
        raise ValueError('posted reply text readback mismatch')
    aware(item['handledAt'], 'postedReplies.handledAt')


def validate_result(result):
    if not isinstance(result, dict):
        raise ValueError('result must be an object')
    for field in ('runId', 'inboxRunId', 'checkedAt'):
        if not isinstance(result.get(field), str) or not result[field].strip():
            raise ValueError(f'{field} is required')
    aware(result['checkedAt'], 'checkedAt')
    if result['runId'] != result['inboxRunId']:
        raise ValueError('new inbox batches require runId == inboxRunId')
    validate_sources(result.get('sources'))
    for name in ARRAYS:
        if not isinstance(result.get(name), list):
            raise ValueError(f'{name} must be an array')
    if len(result['postedReplies']) > 2:
        raise ValueError('at most two postedReplies are allowed per run')
    for name in ARRAYS:
        for item in result[name]:
            if not isinstance(item, dict):
                raise ValueError(f'{name} entries must be objects')
            if item.get('source') not in ALLOWED_SOURCES:
                raise ValueError(f'{name} entry source is invalid')
            if not item_key(item):
                raise ValueError(f'{name} entries need a stable key')
            action = item.get('action')
            if action not in ARRAY_ACTIONS[name]:
                raise ValueError(f'{name} entry action is invalid')
            if name == 'postedReplies':
                validate_posted(item)
    return result


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def normalize_handled(handled):
    result = {}
    for key, value in (handled or {}).items():
        url = norm_permalink(key) or key
        old = result.get(url, {})
        result[url] = {**old, **(value if isinstance(value, dict) else {}), 'permalink': url}
    return result


def acquire_lock(lock_path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open('a+', encoding='utf-8')
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError('another Reddit monitor/inbox run is already active')
    return handle


def persist(result_file):
    state_path, cache_path, deferred_path = _storage_paths()
    result = validate_result(read_json(result_file))
    with acquire_lock(state_path.parent / 'run.lock'):
        state = read_json(state_path)
        inbox = state.setdefault('inbox', {})
        cache = read_json(cache_path) if cache_path.exists() else {}
        deferred_existing = read_json(deferred_path) if deferred_path.exists() else {}
        existing_ids = [existing.get('inboxRunId') for existing in (inbox, cache, deferred_existing)]
        if any(existing_ids) and (not all(existing_ids) or len(set(existing_ids)) != 1):
            raise RuntimeError('state/cache/deferred inboxRunId mismatch')
        old_result = inbox.get('lastResults')
        if old_result and old_result.get('runId') == result['runId']:
            if (canonical(old_result) != canonical(result) or canonical(cache) != canonical(result)
                    or deferred_existing != inbox.get('_deferredReceipt')
                    or inbox.get('handled', {}) != inbox.get('_handledReceipt')):
                raise RuntimeError('same runId with different payload rejected')
            print(json.dumps({'status': 'ok', 'idempotent': True, 'runId': result['runId']}, ensure_ascii=False))
            return
        inbox['inboxRunId'] = result['inboxRunId']
        inbox['handled'] = normalize_handled(inbox.get('handled', {}))
        for section in ('handled', 'noise', 'skippedInsufficientInfo', 'postedReplies'):
            for item in result[section]:
                url = norm_permalink(item.get('permalink') or item.get('parentPermalink') or item.get('commentPermalink'))
                if not url:
                    url = item_key(item)
                old = inbox['handled'].get(url, {})
                if old.get('action') == 'posted_reply' and section != 'postedReplies':
                    continue
                action = item.get('action') or ('posted_reply' if section == 'postedReplies' else None)
                if action not in ALLOWED_ACTIONS:
                    continue
                inbox['handled'][url] = {**old, **item, 'action': action, 'handledAt': item.get('handledAt') or result['checkedAt'], 'permalink': url}
        existing_deferred = {item_key(x): x for x in deferred_existing.get('pending', [])}
        for item in result['deferred']:
            key = item_key(item)
            if key in inbox['handled'] or any(item_key(old) == key for old in inbox['handled'].values()):
                raise RuntimeError(f'deferred item conflicts with handled item: {key}')
            existing_deferred.setdefault(key, item)
        for item in result['handled'] + result['noise'] + result['skippedInsufficientInfo'] + result['postedReplies']:
            existing_deferred.pop(item_key(item), None)
        deferred_doc = {'inboxRunId': result['inboxRunId'], 'pending': list(existing_deferred.values())}
        inbox['lastAttemptedAt'] = result['checkedAt']
        if any(e['status'] == 'verified' for e in result['sources'].values()):
            inbox['lastCheckedAt'] = result['checkedAt']
        inbox['lastResults'] = result
        inbox['_deferredReceipt'] = json.loads(json.dumps(deferred_doc))
        inbox['_handledReceipt'] = json.loads(json.dumps(inbox['handled']))
        cache = {**result, 'inboxRunId': result['inboxRunId']}
        atomic_write_verified(state_path, state)
        atomic_write_verified(deferred_path, deferred_doc)
        atomic_write_verified(cache_path, cache)
        back = read_json(state_path)
        if back != state or read_json(cache_path) != cache or read_json(deferred_path) != deferred_doc:
            raise RuntimeError('state/cache/deferred complete read-back failed')
    print(json.dumps({'status': 'ok', 'runId': result['runId'], 'inboxRunId': result['inboxRunId'], 'handledCount': len(back['inbox']['handled']), 'deferredCount': len(deferred_doc['pending'])}, ensure_ascii=False))


def _status_locked(state_path, cache_path, deferred_path):
    state = read_json(state_path)
    inbox = state.get('inbox', {})
    cache = read_json(cache_path) if cache_path.exists() else {}
    deferred_doc = read_json(deferred_path) if deferred_path.exists() else {}
    ids = [value.get('inboxRunId') for value in (inbox, cache, deferred_doc)]
    if any(ids) and (not all(ids) or len(set(ids)) != 1):
        raise RuntimeError('state/cache/deferred inboxRunId mismatch')
    deferred = deferred_doc.get('pending', [])
    print(json.dumps({'status': 'ok', 'statePath': str(state_path), 'inboxCachePath': str(cache_path), 'lastCheckedAt': inbox.get('lastCheckedAt'), 'handledCount': len(inbox.get('handled', {})), 'deferred': deferred, 'deferredCount': len(deferred), 'fixedIdentity': os.getenv('CAMOFOX_USER_ID', '').strip() or None, 'expectedRedditUser': os.getenv('REDDIT_EXPECTED_USER', '').strip() or None}, ensure_ascii=False, indent=2))


def status():
    state_path, cache_path, deferred_path = _storage_paths()
    with acquire_lock(state_path.parent / 'run.lock'):
        return _status_locked(state_path, cache_path, deferred_path)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    persist_parser = sub.add_parser('persist')
    persist_parser.add_argument('--result-file', required=True, type=Path)
    args = parser.parse_args()
    status() if args.command == 'status' else persist(args.result_file)


if __name__ == '__main__':
    main()
