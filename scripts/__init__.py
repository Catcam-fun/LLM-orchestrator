"""scripts/ — vault Python source root.

Marked as a package so `from scripts.ai_dougs.X import Y` and
`from scripts.vault_graph.X import Y` both work as standard imports.

Existing code uses the older `sys.path.insert(0, 'scripts')` pattern in
vault.py + run_vault.py + ported.py; that still works because Python is
fine with both styles. Added 2026-05-09 after auto_0066's verification
failed solely because the planner emitted `from scripts.X import` (the
modern style) without scripts/ being a package — strictly additive fix
that makes both styles work.
"""
