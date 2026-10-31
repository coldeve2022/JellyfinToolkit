"""Developer entry point; profile selection stays outside release code defaults."""
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[2]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))


def run(argv=None):
    from main import main
    args = list(sys.argv[1:] if argv is None else argv)
    if '--data-dir' not in args and not any(a.startswith('--data-dir=') for a in args):
        args = ['--data-dir', str(PROJECT.parent / 'JellyfinToolkit-Data'), *args]
    return main(args)


if __name__ == '__main__':
    raise SystemExit(run())
