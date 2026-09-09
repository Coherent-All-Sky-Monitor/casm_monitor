"""Scripts that are shipped to another host and executed there.

Nothing in this package is imported by the service: the files are read as text
and piped into a remote interpreter (``ssh zapdos python3 -``), so they must
stay compatible with that host's python (3.8 on zapdos) rather than ours.
"""

SNAP_READ_REMOTE = "snap_read_remote.py"
