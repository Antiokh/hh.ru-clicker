"""Runtime-only cancellation checks for account mutations (never serialized)."""
from functools import wraps
import inspect
from contextlib import nullcontext


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
    def chat_attempt(args, kwargs):
        if method.__name__ in ('send_message', 'send_workflow_event'):
            from app.message_quarantine import attempt
            chat_id = args[0] if args else kwargs.get('neg_id', kwargs.get('chat_id'))
            return attempt(acc, chat_id, method.__name__, args, kwargs)
        return nullcontext({})

    if inspect.iscoroutinefunction(method):
        @wraps(method)
        async def run(*args, **kwargs):
            ensure_mutation_allowed(acc)
            with chat_attempt(args, kwargs) as outcome:
                ensure_mutation_allowed(acc)
                result = await method(*args, **kwargs)
                outcome['confirmed'] = result is True
                return result
    else:
        @wraps(method)
        def run(*args, **kwargs):
            ensure_mutation_allowed(acc)
            with chat_attempt(args, kwargs) as outcome:
                ensure_mutation_allowed(acc)
                result = method(*args, **kwargs)
                outcome['confirmed'] = result is True
                return result
    return run
