"""Analysis engines: consumers of the normalized Event table (ARCHITECTURE.md §6).

Everything in this package is *derived* computation over source-derived events.
Nothing here writes to the events table or the evidence store; outputs are
findings, exports, and report content, always labeled as analyst-derived.
"""
