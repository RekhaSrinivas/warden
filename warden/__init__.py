"""Warden: a guardian that sits between AI agents and a company's money.

Agents only propose payments. Warden checks each proposal against a mandate
written in code, asks a risk reader (Claude) to flag what code cannot see,
decides in code, executes on a payment rail, and records everything in a
hash-chained audit log.
"""
