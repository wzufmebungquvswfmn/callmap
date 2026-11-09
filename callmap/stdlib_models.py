"""Small explicit models for stdlib factories without Python return annotations.

These are conservative type contracts, not executed library code. All inferred
edges remain candidates; Logger subclasses may be configured at runtime.
References: docs.python.org/3/library/logging.html and contextvars.html.
"""

RETURNS = {
    "logging.getLogger": "logging.Logger",
    "logging.Logger.getChild": "logging.Logger",
    "contextvars.copy_context": "contextvars.Context",
}

CLASSES = {
    "contextvars.ContextVar": {
        "get": "get(default=…) — Return the current value or the default.",
        "set": "set(value) — Set a value and return a token for reset().",
        "reset": "reset(token) — Restore the value from a token.",
    },
    "contextvars.Context": {
        "run": "run(callable, *args, **kwargs) — Run in this context.",
        "get": "get(key, default=None) — Return a context value.",
        "copy": "copy() — Return a shallow copy of the context.",
        "items": "items() — Return context variable/value pairs.",
        "keys": "keys() — Return context variables.",
        "values": "values() — Return context values.",
    },
}

# Recognize only declared stdlib interfaces, never arbitrary methods named map.
EXECUTOR_CLASSES = {
    "concurrent.futures.Executor",
    "concurrent.futures._base.Executor",
    "concurrent.futures.ThreadPoolExecutor",
    "concurrent.futures.thread.ThreadPoolExecutor",
    "concurrent.futures.ProcessPoolExecutor",
    "concurrent.futures.process.ProcessPoolExecutor",
}
EXECUTOR_CALLBACKS = {
    f"{owner}.{method}": method
    for owner in EXECUTOR_CLASSES
    for method in ("map", "submit")
}

# (callable positional index, parameter name, execution role). These contracts
# require a qualified stdlib API; a business method named submit is not enough.
EXECUTION_CALLBACKS = {
    "asyncio.to_thread": (0, "func", "thread"),
    "asyncio.threads.to_thread": (0, "func", "thread"),
    "asyncio.BaseEventLoop.run_in_executor": (1, "func", "executor"),
    "asyncio.base_events.BaseEventLoop.run_in_executor": (1, "func", "executor"),
    "contextvars.Context.run": (0, "callable", "context"),
}
COROUTINE_SCHEDULERS = {
    "asyncio.create_task",
    "asyncio.tasks.create_task",
    "asyncio.ensure_future",
    "asyncio.tasks.ensure_future",
    "asyncio.BaseEventLoop.create_task",
    "asyncio.base_events.BaseEventLoop.create_task",
}
RETURNS.update(
    {
        "asyncio.get_running_loop": "asyncio.BaseEventLoop",
        "asyncio.get_event_loop": "asyncio.BaseEventLoop",
        "asyncio.events.get_running_loop": "asyncio.BaseEventLoop",
        "asyncio.events.get_event_loop": "asyncio.BaseEventLoop",
    }
)
CLASSES["asyncio.BaseEventLoop"] = {
    "run_in_executor": "run_in_executor(executor, func, *args)"
    " — Arrange for func to be called in the executor.",
    "create_task": "create_task(coro, *, name=None, context=None)"
    " — Schedule execution of the coroutine.",
}
CLASSES.update(
    {
        owner: {
            "map": "map(fn, *iterables, timeout=None, chunksize=1)"
            " — Schedule fn for iterable items.",
            "submit": "submit(fn, *args, **kwargs)"
            " — Schedule fn with supplied arguments.",
            "__enter__": "__enter__() — Return this executor.",
        }
        for owner in EXECUTOR_CLASSES
    }
)
