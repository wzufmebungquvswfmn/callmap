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
