"""Orchestration.

Sequences the domain rules and talks to collaborators through the protocols in
`domain.ports` — never to a vendor SDK directly. That is what lets the whole
ingestion path run against fakes in tests with no credentials.
"""
