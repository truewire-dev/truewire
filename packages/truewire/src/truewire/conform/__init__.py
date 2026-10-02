"""`truewire conform`: replay every recorded request live and report drift (ADR 0012).

`run.Conform` makes the calls, `shape` turns a response into findings, `report` writes the
report and keeps the ledger of first-seen dates.
"""
