"""Business rules. Pure Python.

No Azure SDK, no database driver, no web framework. If this package needs one
of those to import, something has leaked into it — and the financial logic
stops being testable on its own.

Everything that decides a monetary figure lives here: the models, the
consistency gates, the rate comparison, and document identity.
"""
