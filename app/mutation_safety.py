"""Runtime-only cancellation checks for account mutations (never serialized)."""
from functools import wraps
import inspect


class MutationBlocked(RuntimeError):
    pass


class OutcomeUnknown(RuntimeError):
    outcome_unknown = True


MUTATING_METHODS = frozenset({
    "send_message", "send_workflow_event", "send_participant_action", "mark_chat_read",
    "auto_decline_discards", "submit_response", "fill_questionnaire", "touch_resume",
    "edit_resume_field", "set_job_search_status", "start_hedi",
})


def ensure_mutation_allowed(acc):
    guard = acc.get("_mutation_guard")
    if guard is not None and not guard():
        raise MutationBlocked("Операция отменена: аккаунт остановлен, удалён или на паузе")


def guarded_method(method, acc):
    def check_chat(args, kwargs):
        if method.__name__ in ('send_message', 'send_workflow_event'):
            from app.message_quarantine import blocked
            chat_id = args[0] if args else kwargs.get('neg_id', kwargs.get('chat_id'))
            if blocked(acc, chat_id):
                raise MutationBlocked('Чат изолирован: результат предыдущего сообщения неизвестен')

    if inspect.iscoroutinefunction(method):
        @wraps(method)
        async def run(*args, **kwargs):
            ensure_mutation_allowed(acc)
            check_chat(args, kwargs)
            return await method(*args, **kwargs)
    else:
        @wraps(method)
        def run(*args, **kwargs):
            ensure_mutation_allowed(acc)
            check_chat(args, kwargs)
            return method(*args, **kwargs)
    return run
