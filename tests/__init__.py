"""Makes `python -m unittest discover -s tests -t .` work as well as the explicit runner.

Without this the discovery start directory is not importable and the run fails before a
single test executes.
"""
