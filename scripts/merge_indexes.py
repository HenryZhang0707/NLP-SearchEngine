"""Repository entry point for the offline shard merger."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from offline.merge_indexes import main

if __name__ == "__main__":
    raise SystemExit(main())
