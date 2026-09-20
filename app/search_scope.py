"""Explicit remote/IT scope based on HH's professional role taxonomy."""
from urllib.parse import urlsplit, urlunsplit, parse_qs, urlencode

# api.hh.ru/professional_roles, category 11, verified 2026-09-07.
IT_ROLES = frozenset('156 160 10 12 150 25 165 34 36 73 155 96 164 104 157 107 112 113 148 114 116 121 124 125 126'.split())


def remote_it_filters(filters):
    return {**filters, 'professional_role': sorted(IT_ROLES, key=int),
            'schedule': 'remote', 'work_format': 'REMOTE'}


def remote_it_url(url):
    parts = urlsplit(url)
    query = remote_it_filters(parse_qs(parts.query, keep_blank_values=True))
    return urlunsplit(parts._replace(query=urlencode(query, doseq=True)))


def scope_metadata(item):
    return {key: item.get(key) for key in ('professional_roles', 'work_format', 'schedule')}


def remote_it_rejection(meta):
    roles = meta.get('professional_roles')
    if not isinstance(roles, list) or not roles:
        return 'scope_unknown'
    ids = {str(role.get('id')) for role in roles if isinstance(role, dict)}
    if not ids & IT_ROLES:
        return 'not_it'
    formats = meta.get('work_format')
    if isinstance(formats, list) and formats:
        return None if any(isinstance(f, dict) and f.get('id') == 'REMOTE' for f in formats) else 'not_remote'
    schedule = meta.get('schedule')
    if isinstance(schedule, dict) and schedule.get('id'):
        return None if schedule['id'] == 'remote' else 'not_remote'
    return 'scope_unknown'
