"""Outreach services (Phase 3): templates, decks, research, AI drafting and the message state machine.

``messages`` is the ONLY module that sends email. No scheduler, job or agent may import
``messages.send`` (enforced by ``tests/unit/outreach/test_no_autosend.py``).
"""
