"""Copy a closed profile to a new directory; never merge or overwrite a target."""
from pathlib import Path
import argparse
import json
import sys

PROJECT = Path(__file__).resolve().parents[2]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='已有资料库根目录')
    parser.add_argument('--target', required=True, help='尚不存在的新资料库根目录')
    args = parser.parse_args(argv)
    from utils.profile_migration import migrate_profile
    report = migrate_profile(args.source, args.target,
        progress=lambda state: print(json.dumps(state, ensure_ascii=True), flush=True))
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
