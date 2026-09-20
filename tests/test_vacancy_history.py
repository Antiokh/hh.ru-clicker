import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock

import pytest
from app import vacancy_history as history
from app.manager import _is_fresh_vacancy


@pytest.fixture
def observed(tmp_path, monkeypatch):
    monkeypatch.setattr(history, 'HISTORY_FILE', tmp_path / 'history.json')
    return datetime(2026, 9, 5, 12, tzinfo=timezone.utc)


def test_updated_date_does_not_spend_reserve(observed):
    old = observed - timedelta(days=5)
    history.observe({'123': {'published_at': old.isoformat()}}, now=old)
    vacancies = {'123': {'published_at': observed.isoformat()}}
    history.observe(vacancies, now=observed)
    assert vacancies['123']['publication_updated']
    assert not _is_fresh_vacancy(vacancies['123'], 24, observed)
    assert vacancies['123']['first_observed_at'] == old.isoformat()


def test_first_observation_not_republication(observed):
    vacancies = {'123': {'published_at': observed.isoformat()}}
    history.observe(vacancies, now=observed)
    history.observe(vacancies, now=observed + timedelta(minutes=1))
    assert not vacancies['123']['publication_updated']
    assert _is_fresh_vacancy(vacancies['123'], 24, observed)


def test_unknown_date_not_fresh(observed):
    vacancies = {'123': {}}
    history.observe(vacancies, now=observed)
    assert not _is_fresh_vacancy(vacancies['123'], 24, observed)


def test_history_contains_no_contacts_or_resume(observed):
    history.observe({'123': {'contacts': 'private', 'resume_hash': 'secret'}}, now=observed)
    saved = history.HISTORY_FILE.read_text()
    assert 'private' not in saved and 'secret' not in saved


@pytest.mark.parametrize('contacts,expected', [(None, False), ({}, False), ({'email':'synthetic@example.test'}, True)])
def test_contact_check_read_only(monkeypatch, contacts, expected):
    from app.routes import discovery
    request = Mock(return_value={'id':'123', 'contacts':contacts})
    monkeypatch.setattr(discovery, '_acc', lambda idx: {'name':'synthetic'})
    monkeypatch.setattr(discovery, 'mobile_request', request)
    result = asyncio.run(discovery.api_vacancy_contact(0, '123'))
    assert result['contact_available'] is expected
    request.assert_called_once_with({'name':'synthetic'}, 'GET', '/vacancies/123')
    assert 'synthetic@example.test' not in str(result)
