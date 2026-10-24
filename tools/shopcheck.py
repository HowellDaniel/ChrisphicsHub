"""One rule for the checks that start their own engine: never run against a port that is already
somebody else's.

A server left behind by a run that crashed answers /healthz just as readily as the one this check
meant to start, and every request then goes to its stale book — the figures can look perfect while
nothing is being proved, and a check that writes could write into a book it never meant to touch.
Asking whether anything *listens* (rather than whether the port binds) leaves a port in TIME_WAIT
alone: nothing listens there, so the engine that follows can still take it.
"""
import socket
import sys


def in_use(port, host="127.0.0.1", timeout=0.5):
    probe = socket.socket()
    probe.settimeout(timeout)
    try:
        return probe.connect_ex((host, port)) == 0
    finally:
        probe.close()


def require_free(*ports):
    for port in ports:
        if in_use(port):
            print("port %d is already answered by something else — a check that runs against a "
                  "port someone else is holding proves nothing." % port, file=sys.stderr)
            print("Find it with: lsof -nP -iTCP:%d -sTCP:LISTEN" % port, file=sys.stderr)
            # 2, not 1: refusing to run is not the same as a check that ran and failed.
            raise SystemExit(2)
