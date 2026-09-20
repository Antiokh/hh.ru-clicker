"""Search follows HH pagination within its accessible window, without sends."""
import pytest
from app.mobile_search import search_vacancies


@pytest.mark.parametrize('per_page, expected', [(50, 40), (20, 100), (100, 20)])
def test_default_reaches_hh_window_not_old_twenty_page_cap(monkeypatch, per_page, expected):
    calls = []
    def request(acc, method, endpoint, params):
        assert method == 'GET'
        calls.append(params['page'])
        return {'pages': 999, 'found': 9999, 'items': [{'id': str(params['page'])}]}
    monkeypatch.setattr('app.mobile_search.mobile_request', request)
    result = search_vacancies({}, 'synthetic', per_page=per_page)
    assert calls == list(range(expected))
    assert result.pagination == {'pages_loaded': expected, 'stop_reason': 'hh_limit'}


@pytest.mark.parametrize('kind, expected, reason', [('end', 3, 'end'), ('empty', 2, 'end'),
    ('repeat', 2, 'repeated_page'), ('missing', 1, 'missing_pagination')])
def test_detects_end_and_invalid_pagination(monkeypatch, kind, expected, reason):
    calls = []
    def request(acc, method, endpoint, params):
        page = params['page']; calls.append(page)
        return {'pages': 0 if kind == 'missing' else 3,
                'items': [] if kind == 'empty' and page else [{'id': str(0 if kind == 'repeat' else page)}]}
    monkeypatch.setattr('app.mobile_search.mobile_request', request)
    result = search_vacancies({}, 'synthetic')
    assert len(calls) == expected
    assert result.pagination['stop_reason'] == reason


def test_pause_prevents_next_page(monkeypatch):
    calls = []
    monkeypatch.setattr('app.mobile_search.mobile_request', lambda *a, **kw:
        calls.append(1) or {'pages': 40, 'items': [{'id':'1'}]})
    result = search_vacancies({'_search_guard': lambda: not calls}, 'synthetic')
    assert len(calls) == 1
    assert result.pagination['stop_reason'] == 'cancelled'


def test_configured_budget_is_reported_as_partial(monkeypatch):
    monkeypatch.setattr('app.mobile_search.mobile_request', lambda *a, **kw:
        {'pages':40, 'items':[{'id':'1'}]})
    result = search_vacancies({}, 'synthetic', max_pages=1)
    assert result.pagination['stop_reason'] == 'configured_limit'


def test_reported_final_page_at_hh_ceiling_is_not_entire_market(monkeypatch):
    monkeypatch.setattr('app.mobile_search.mobile_request', lambda *a, **kw:
        {'pages':40, 'found':3000, 'items':[{'id':str(kw['params']['page'])}]})
    result = search_vacancies({}, 'synthetic', per_page=50)
    assert result.pagination['stop_reason'] == 'hh_limit'


@pytest.mark.parametrize('reason, partial', [('end', False), ('hh_limit', True), ('configured_limit', True)])
def test_manager_exports_actual_page_count_and_partial_search(monkeypatch, reason, partial):
    from types import SimpleNamespace
    from app.manager import BotManager
    from app.mobile_search import SearchResults
    from app.cycle_report import begin_cycle, cycle_snapshot, found_cycle
    state = SimpleNamespace(acc={'mode':'oauth', 'urls':['https://hh.ru/search/vacancy?text=synthetic']},
        _deleted=False, short='test', status_detail='', vacancy_meta={})
    begin_cycle(state)
    items = SearchResults()
    items.pagination = {'pages_loaded': 37, 'stop_reason': reason}
    def search(*args, **kwargs):
        assert kwargs['max_pages'] == 100
        return items
    monkeypatch.setattr('app.manager.CONFIG.pages_per_url', 100)
    monkeypatch.setattr('app.manager.get_client', lambda acc: SimpleNamespace(search_vacancies=search))
    monkeypatch.setattr('app.vacancy_history.observe', lambda *args: None)
    BotManager.__new__(BotManager)._collect_via_oauth_api(state)
    found_cycle(state, [], 0)
    snapshot = cycle_snapshot(state)
    assert snapshot['search_pages_loaded'] == 37
    assert snapshot['partial'] is partial


def test_resume_endpoint_can_read_beyond_twenty_pages(monkeypatch):
    calls=[]
    def request(acc, method, endpoint, params):
        assert endpoint == '/resumes/synthetic/similar_vacancies'
        assert params['per_page'] == 20
        calls.append(params['page'])
        return {'pages': 25, 'items':[{'id':str(params['page'])}]}
    monkeypatch.setattr('app.mobile_search.mobile_request', request)
    result = search_vacancies({}, 'x', per_page=50, filters={'resume':'synthetic'})
    assert len(calls) == 25 and result.pagination['stop_reason'] == 'end'


def test_web_fallback_stops_at_repeated_page(monkeypatch):
    from types import SimpleNamespace
    from app.hh_api import fetch_hh_vacancies
    calls=[]
    def get(*a, **kw):
        calls.append(1)
        return SimpleNamespace(text='<a href="/vacancy/123">test</a>', raise_for_status=lambda:None)
    monkeypatch.setattr('app.hh_http.HH.get', get)
    result=fetch_hh_vacancies({}, 'x')
    assert len(calls)==2 and len(result)==1
    assert result.pagination['stop_reason']=='repeated_page'
