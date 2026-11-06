"""Provision private Slack channels only for a durable explicit setup request."""
from __future__ import annotations

import asyncio
import time

import config_edit
import slack_channel_setup as setup


def _payload(value):
    data = getattr(value, 'data', value)
    return data if isinstance(data, dict) else {}


async def _call(client, method, **kwargs):
    try:
        response = _payload(await getattr(client, method)(**kwargs))
    except Exception as error:
        response = _payload(getattr(error, 'response', {}))
        if not response:
            return {'ok': False, 'error': 'response_unknown'}
    return response


def _channel(value, channel_id, user):
    return (isinstance(value, dict) and value.get('id') == channel_id
            and value.get('is_private') is True and value.get('is_archived') is False
            and value.get('is_member') is True
            and value.get('is_shared') is not True and value.get('is_ext_shared') is not True
            and bool(user))


async def _consume(root, client, binding):
    """Use the caller-owned native client; never allocate credentials or another client."""
    with config_edit._edit_lock(str(setup.path(root))):
        state = setup.read(root)
        if state is None or state.get('state') in ('verified', 'applied'):
            return state
        plan = state.get('plan', {})
        if binding != plan.get('binding'):
            return {'state': 'blocked', 'reason': 'slack_setup_binding_mismatch'}
        identity = await _call(client, 'auth_test')
        if (identity.get('ok') is not True or identity.get('team_id') != binding['team_id']
                or not identity.get('bot_id') or not identity.get('user_id')):
            state.update(state='blocked', reason='slack_setup_identity_unverified')
            setup.save(root, state)
            return state
        bot = await _call(client, 'bots_info', bot=identity['bot_id'])
        if (bot.get('ok') is not True or (bot.get('bot') or {}).get('app_id') != binding['application_id']
                or (bot.get('bot') or {}).get('id') != identity['bot_id']
                or (bot.get('bot') or {}).get('deleted') is not False):
            state.update(state='blocked', reason='slack_setup_identity_unverified')
            setup.save(root, state)
            return state
        deadline = time.monotonic() + 40
        for role in ('patient', 'system'):
            if time.monotonic() >= deadline:
                raise ValueError('slack_setup_deadline')
            record = state['roles'][role]
            if record['state'] == 'pending':
                record['state'] = 'creating'
                setup.save(root, state)  # write-before-send: unknown never creates again
                response = await _call(client, 'conversations_create', name=record['name'], is_private=True)
                channel = response.get('channel') or {}
                if response.get('ok') is not True or not channel.get('id'):
                    record['state'] = 'unknown' if response.get('error') == 'response_unknown' else 'blocked'
                    state.update(state=record['state'], reason=(
                        'slack_setup_scope_required' if response.get('error') == 'missing_scope'
                        else 'slack_setup_name_conflict' if response.get('error') == 'name_taken'
                        else 'slack_setup_create_unverified'))
                    setup.save(root, state)
                    return state
                record.update(id=channel['id'], state='created', create_receipt={
                    'request_id': state['request_id'], 'channel_id': channel['id'],
                    'team_id': binding['team_id'], 'bot_id': identity['bot_id']})
                setup.save(root, state)
            if not record.get('id') and record['state'] == 'blocked':
                return state
            if not record.get('id'):
                state.update(state='unknown', reason='slack_setup_create_unverified')
                setup.save(root, state)
                return state
            response = await _call(client, 'conversations_info', channel=record['id'])
            if response.get('ok') is not True or not _channel(response.get('channel'), record['id'], identity['user_id']):
                state.update(state='blocked', reason='slack_setup_channel_unverified')
                setup.save(root, state)
                return state
            if record.get('create_receipt'):
                if record['create_receipt']['bot_id'] != identity['bot_id']:
                    raise ValueError('slack_setup_identity_changed')
                # Membership is reconciled before any invite, including after a lost response.
                found, cursor, complete = set(), None, False
                for _ in range(20):
                    if time.monotonic() >= deadline:
                        raise ValueError('slack_setup_deadline')
                    response = await _call(client, 'conversations_members', channel=record['id'], limit=200,
                                           **({'cursor': cursor} if cursor else {}))
                    if response.get('ok') is not True or not isinstance(response.get('members'), list):
                        break
                    found.update(response['members'])
                    cursor = (response.get('response_metadata') or {}).get('next_cursor')
                    if not cursor:
                        complete = True
                        break
                else:
                    cursor = 'incomplete'
                if not complete:
                    state.update(state='blocked', reason='slack_setup_members_unverified')
                    setup.save(root, state)
                    return state
                missing = sorted(set(plan['users']) - found)
                if missing:
                    if record.get('invite_state') == 'sending':
                        state.update(state='unknown', reason='slack_setup_invite_unverified')
                        setup.save(root, state)
                        return state
                    record['invite_state'] = 'sending'
                    setup.save(root, state)
                    response = await _call(client, 'conversations_invite', channel=record['id'], users=','.join(missing))
                    if response.get('ok') is not True:
                        state.update(state='unknown', reason='slack_setup_invite_unverified')
                        setup.save(root, state)
                        return state
                record['invite_state'] = 'confirmed'
            record['state'] = 'verified'
            setup.save(root, state)
        if state['roles']['patient']['id'] == state['roles']['system']['id']:
            state.update(state='blocked', reason='slack_setup_channels_not_distinct')
        else:
            state.update(state='verified', reason=None)
        setup.save(root, state)
        return state


async def consume(root, client, binding):
    """Bound the whole bootstrap and preserve any interrupted external write as unknown."""
    try:
        return await asyncio.wait_for(_consume(root, client, binding), timeout=45)
    except (asyncio.CancelledError, TimeoutError):
        with config_edit._edit_lock(str(setup.path(root))):
            state = setup.read(root)
            if state and state.get('state') != 'verified':
                for record in state['roles'].values():
                    if record['state'] == 'creating':
                        record['state'] = 'unknown'
                state.update(state='unknown', reason='slack_setup_response_unknown')
                setup.save(root, state)
        raise
