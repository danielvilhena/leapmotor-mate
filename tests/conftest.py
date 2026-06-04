"""Put poller/ and web/ on sys.path so tests can import their modules by bare name
(the two dirs are separate import roots in the container, mirrored here)."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _d in ("poller", "web"):
    p = str(ROOT / _d)
    if p not in sys.path:
        sys.path.insert(0, p)
