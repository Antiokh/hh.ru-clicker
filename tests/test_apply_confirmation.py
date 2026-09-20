"""Synthetic evidence only: no real tokens, accounts, or requests."""
import json
import threading
import time
from unittest.mock import Mock

import pytest

from app import apply_confirmation as confirmation
from app import oauth, hh_http


@pytest.fixture
def receipt_env(monkeypatch):
    acc = {'resume_hash': 'resume1', 'user_id': 'owner1', 'short': 'synthetic', 'cookies': {}}
    record = {'access_token': 'synthetic-token', 'expires_at': time.time() + 1000,
              'source': 'mobile_otp', 'mobile_user_id': 'owner1'}
    monkeypatch.setattr(oauth, '_oauth_tokens', {oauth._token_key(acc): record})
    monkeypatch.setattr(oauth, '_obtain_oauth_token', Mock(side_effect=AssertionError('no authorization')))
    monkeypatch.setattr(confirmation, 'mobile_headers', lambda *args: {'Authorization': 'Bearer synthetic-token'})
    monkeypatch.setattr(hh_http, '_PROXY', 'http://proxy.test:3128')
    get = Mock(side_effect=AssertionError('response fixture required'))
    monkeypatch.setattr(confirmation.requests.Session, 'get', get)
    return acc, record, get


def response(payload=None, *, status=200, chunks=None):
    result = Mock(status_code=status)
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.iter_content = Mock(return_value=iter(chunks if chunks is not None else [json.dumps(payload).encode()]))
    return result


def topic(**changes):
    return {'id': 'topic1', 'vacancy': {'id': 'vacancy1'}, 'resume': {'id': 'resume1'},
            'state': {'id': 'response'}, **changes}


def confirm(acc):
    return confirmation.confirm_application_receipt(acc, 'vacancy1', 'resume1')


def test_exact_receipt_one_fresh_get_with_no_redirects(receipt_env):
    acc, _, get = receipt_env
    get.side_effect = [response({'items': [topic()]})]
    assert confirm(acc) == {'negotiation_id': 'topic1', 'receipt_confirmed': True}
    assert get.call_count == 1
    args, kwargs = get.call_args
    assert args == ('https://api.hh.ru/negotiations',)
    assert kwargs['params'] == {'vacancy_id': 'vacancy1', 'page': 0, 'per_page': 100}
    assert kwargs['allow_redirects'] is False and kwargs['stream'] is True
    assert 'cookies' not in kwargs
    oauth._obtain_oauth_token.assert_not_called()


def test_targeted_lookup_finds_receipt_outside_first_global_page(receipt_env):
    acc, _, get = receipt_env
    def lookup(*args, **kwargs):
        if kwargs['params'].get('vacancy_id') == 'vacancy1':
            return response({'items': [topic(created_at='2020-01-01T00:00:00+0300')], 'pages': 1})
        return response({'items': [topic(id='other' + str(i), vacancy={'id': 'unrelated'})
                                   for i in range(100)], 'pages': 242})
    get.side_effect = lookup
    receipt = confirm(acc)
    assert receipt['receipt_confirmed'] is True
    assert receipt['receipt_created_at'] == '2020-01-01T00:00:00+03:00'
    assert get.call_count == 1
    assert 'status' not in get.call_args.kwargs['params']


@pytest.mark.parametrize('item', [
    topic(vacancy={'id': 'other'}), topic(resume={'id': 'other'}),
    topic(id=None), topic(state={'id': 'interview'}), topic(state=None),
    topic(state={'id': 'invitation'}), topic(state={'id': 'discard'}), topic(state={'id': 'hidden'}),
    topic(resume={}), topic(resume={'id': True}),
])
def test_inexact_or_invitation_is_not_receipt(receipt_env, item):
    acc, _, get = receipt_env
    get.side_effect = [response({'items': [item]})]
    assert confirm(acc) is None
    assert get.call_count == 1


@pytest.mark.parametrize('question_state', [True, None, 0, 'false', {'id': 'answered'}])
def test_pre_application_question_or_unknown_flag_cannot_confirm(receipt_env, question_state):
    acc, _, get = receipt_env
    get.side_effect = [response({'items': [topic(applicant_question_state=question_state)]})]
    assert confirm(acc) is None
    assert get.call_count == 1


def test_native_false_question_state_confirms(receipt_env):
    acc, _, get = receipt_env
    get.side_effect = [response({'items': [topic(applicant_question_state=False)]})]
    assert confirm(acc)['receipt_confirmed'] is True


def test_missing_resume_requires_one_exact_topic_detail(receipt_env):
    acc, _, get = receipt_env
    get.side_effect = [response({'items': [topic(resume=None)]}), response(topic())]
    assert confirm(acc)['receipt_confirmed'] is True
    assert get.call_count == 2
    assert get.call_args.args == ('https://api.hh.ru/negotiations/topic1',)


@pytest.mark.parametrize('created,expected', [
    ('2026-01-02T03:04:05+0300', '2026-01-02T03:04:05+03:00'),
    ('2026-01-02T03:04:05Z', '2026-01-02T03:04:05+00:00'),
    ('2026-01-02T03:04:05', None), ('not a timestamp', None), (None, None),
])
def test_receipt_date_is_optional_validated_aware_timestamp(receipt_env, created, expected):
    acc, _, get = receipt_env
    get.side_effect = [response({'items': [topic(created_at=created)]})]
    result = confirm(acc)
    assert result['receipt_confirmed'] is True
    assert result.get('receipt_created_at') == expected


@pytest.mark.parametrize('detail', [topic(id='other'), topic(resume={'id': 'other'}), topic(resume=None)])
def test_wrong_detail_is_not_receipt(receipt_env, detail):
    acc, _, get = receipt_env
    get.side_effect = [response({'items': [topic(resume=None)]}), response(detail)]
    assert confirm(acc) is None
    assert get.call_count == 2


def test_multiple_unresolved_topics_do_not_expand_read_budget(receipt_env):
    acc, _, get = receipt_env
    get.side_effect = [response({'items': [topic(resume=None), topic(id='topic2', resume=None)]})]
    assert confirm(acc) is None
    assert get.call_count == 1


@pytest.mark.parametrize('status', [301, 302, 401, 403, 429, 500])
def test_http_failure_never_retries_or_refreshes(receipt_env, status):
    acc, _, get = receipt_env
    get.side_effect = [response(status=status)]
    assert confirm(acc) is None
    assert get.call_count == 1
    oauth._obtain_oauth_token.assert_not_called()


def test_absent_match_does_not_walk_all_history(receipt_env):
    acc, _, get = receipt_env
    get.side_effect = [response({'items': [], 'pages': 1000})]
    assert confirm(acc) is None
    assert get.call_count == 1


@pytest.mark.parametrize('item,expected_phase', [
    (topic(vacancy={'id': 'other'}), 'no_matching_vacancy'),
    (topic(state={'id': 'discard'}), 'matching_topic_unconfirmed'),
])
def test_inconclusive_lookup_logs_only_fixed_reason(receipt_env, monkeypatch, item, expected_phase):
    acc, _, get = receipt_env
    logs = []
    monkeypatch.setattr(confirmation, 'log_debug', logs.append)
    get.side_effect = [response({'items': [item]})]
    assert confirm(acc) is None
    output = '\n'.join(logs)
    assert 'phase=' + expected_phase in output
    for forbidden in ('owner1', 'resume1', 'vacancy1', 'topic1', 'https://', 'synthetic-token'):
        assert forbidden not in output


@pytest.mark.parametrize('payload', [None, [], {'items': None}, {'items': [topic()] * 101}])
def test_malformed_payload_is_inconclusive(receipt_env, payload):
    acc, _, get = receipt_env
    get.side_effect = [response(payload)]
    assert confirm(acc) is None


def test_response_bytes_are_bounded(receipt_env, monkeypatch):
    acc, _, get = receipt_env
    monkeypatch.setattr(confirmation, 'MAX_RESPONSE_BYTES', 20)
    get.side_effect = [response(chunks=[b'x' * 21])]
    assert confirm(acc) is None


def test_timeout_is_inconclusive_without_retry(receipt_env):
    acc, _, get = receipt_env
    get.side_effect = TimeoutError('synthetic sensitive-looking text')
    assert confirm(acc) is None
    assert get.call_count == 1


@pytest.mark.parametrize('error_type,label', [
    (confirmation.requests.exceptions.ConnectTimeout, 'connect_timeout'),
    (confirmation.requests.exceptions.ProxyError, 'proxy_error'),
    (confirmation.requests.exceptions.ReadTimeout, 'read_timeout'),
])
def test_diagnostics_never_include_exception_details_or_account_values(receipt_env, monkeypatch, error_type, label):
    acc, _, get = receipt_env
    logs = []
    monkeypatch.setattr(confirmation, 'log_debug', logs.append)
    get.side_effect = error_type('https://secret-user:secret-password@proxy.test/private-path')
    assert confirm(acc) is None
    output = '\n'.join(logs)
    assert 'phase=list_get_failed' in output and 'error_class=' + label in output
    assert 'phase=start' in output and 'phase=end' in output and 'elapsed_ms=' in output
    for forbidden in ('secret-', 'proxy.test', 'private-path', 'owner1', 'resume1', 'vacancy1', 'synthetic-token'):
        assert forbidden not in output
    assert get.call_count == 1


def test_broken_diagnostic_logger_does_not_change_confirmed_result(receipt_env, monkeypatch):
    acc, _, get = receipt_env
    monkeypatch.setattr(confirmation, 'log_debug', Mock(side_effect=RuntimeError('synthetic')))
    get.side_effect = [response({'items': [topic()]})]
    assert confirm(acc)['receipt_confirmed'] is True


@pytest.mark.parametrize('status,reason', [
    (401, 'auth'), (403, 'auth'), (429, 'rate_limit'),
    (302, 'unconfirmed'), (500, 'unconfirmed'), (200, 'unconfirmed'),
])
def test_failure_reason_http_is_fixed_and_survives_end(receipt_env, status, reason):
    acc, _, get = receipt_env
    get.side_effect = [response({'items': []}, status=status)]
    assert confirm(acc) is None
    assert confirmation.receipt_check_failure_reason() == reason
    confirmation._diagnostic('end', time.monotonic())
    assert confirmation.receipt_check_failure_reason() == reason


@pytest.mark.parametrize('error_type,reason', [
    (confirmation.requests.exceptions.ConnectTimeout, 'connect_timeout'),
    (confirmation.requests.exceptions.ReadTimeout, 'read_timeout'),
    (confirmation.requests.exceptions.ProxyError, 'unconfirmed'),
    (confirmation.requests.exceptions.Timeout, 'unconfirmed'),
    (RuntimeError, 'unconfirmed'),
])
def test_failure_reason_never_exposes_exception_text(receipt_env, error_type, reason):
    acc, _, get = receipt_env
    get.side_effect = error_type('https://synthetic-secret:synthetic-password@proxy.test/token')
    assert confirm(acc) is None
    assert confirmation.receipt_check_failure_reason() == reason
    assert get.call_count == 1


@pytest.mark.parametrize('next_result', ['invalid_input', 'no_token', 'no_match', 'confirmed'])
def test_failure_reason_resets_before_every_check(receipt_env, next_result):
    acc, _, get = receipt_env
    get.side_effect = confirmation.requests.exceptions.ConnectTimeout('synthetic')
    assert confirm(acc) is None
    assert confirmation.receipt_check_failure_reason() == 'connect_timeout'
    if next_result == 'invalid_input':
        assert confirm(None) is None
    elif next_result == 'no_token':
        oauth._oauth_tokens.clear()
        assert confirm(acc) is None
    else:
        get.side_effect = [response({'items': [topic()] if next_result == 'confirmed' else []})]
        assert bool(confirm(acc)) is (next_result == 'confirmed')
    assert confirmation.receipt_check_failure_reason() == 'unconfirmed'


def test_failure_reason_is_thread_local(receipt_env, monkeypatch):
    acc, _, get = receipt_env
    monkeypatch.setattr(confirmation, '_receipt_diagnostics', threading.local())
    barrier = threading.Barrier(2)
    results = {}
    errors = {
        'connect': confirmation.requests.exceptions.ConnectTimeout,
        'read': confirmation.requests.exceptions.ReadTimeout,
    }

    def mocked_get(*args, **kwargs):
        raise errors[threading.current_thread().name]('synthetic-secret')

    def check():
        name = threading.current_thread().name
        results[name + '_initial'] = confirmation.receipt_check_failure_reason()
        results[name + '_receipt'] = confirm(acc)
        barrier.wait(timeout=5)
        results[name] = confirmation.receipt_check_failure_reason()

    get.side_effect = mocked_get
    workers = [threading.Thread(target=check, name=name) for name in errors]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        assert not worker.is_alive()
    assert results == {
        'connect_initial': 'unconfirmed', 'connect_receipt': None, 'connect': 'connect_timeout',
        'read_initial': 'unconfirmed', 'read_receipt': None, 'read': 'read_timeout',
    }
    assert confirmation.receipt_check_failure_reason() == 'unconfirmed'


@pytest.mark.parametrize('change', ['owner', 'resume', 'token'])
def test_identity_change_during_get_rejects_receipt(receipt_env, change):
    acc, record, get = receipt_env
    def changed(*args, **kwargs):
        if change == 'owner':
            acc['user_id'] = 'other'
        elif change == 'resume':
            acc['resume_hash'] = 'other'
        else:
            record['access_token'] = 'replacement'
        return response({'items': [topic()]})
    get.side_effect = changed
    assert confirm(acc) is None


def test_pinned_resume_must_match(receipt_env):
    acc, _, get = receipt_env
    acc['_pinned_resume_id'] = 'another'
    assert confirm(acc) is None
    get.assert_not_called()


@pytest.mark.parametrize('kind', ['missing', 'expired', 'wrong_owner'])
def test_no_usable_token_does_not_authorize(receipt_env, kind):
    acc, record, get = receipt_env
    if kind == 'missing':
        oauth._oauth_tokens.clear()
    elif kind == 'expired':
        record['expires_at'] = time.time() - 1
    else:
        record['mobile_user_id'] = 'other'
    assert confirm(acc) is None
    get.assert_not_called()
    oauth._obtain_oauth_token.assert_not_called()


@pytest.mark.parametrize('owner_matches', [True, False])
def test_legacy_plain_mobile_token_requires_matching_owner(receipt_env, owner_matches):
    acc, record, get = receipt_env
    oauth._oauth_tokens.clear()
    oauth._oauth_tokens['resume1'] = record
    if not owner_matches:
        acc.pop('user_id')
    get.side_effect = [response({'items': [topic()]})]
    result = confirm(acc)
    assert bool(result) is owner_matches
    assert get.call_count == int(owner_matches)
