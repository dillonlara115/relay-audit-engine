"""The operator console: a web app for running the engine.

Read-only views live in app.report.dashboard. This package adds the actions,
which is why it carries its own session gate and a CSRF token on every
mutating form.

What it deliberately does not have: a send button. Rule 4 says drafts only, no
automated sending, and a web app is exactly where that rule would quietly erode
if the affordance existed.
"""
