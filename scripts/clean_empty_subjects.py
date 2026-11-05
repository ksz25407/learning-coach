"""One-off cleanup: delete the empty subjects that '+ New subject' used to leave behind.

    python scripts/clean_empty_subjects.py            # only lists them (dry run)
    python scripts/clean_empty_subjects.py --delete   # backs up the database, then deletes them

"Empty" = no answer from you, no profile, no plan, no PDFs, no review cards
(see store.empty_profile_ids). Spend history (usage_log) is kept.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so imports work from scripts/

import config  # noqa: E402
from db import store  # noqa: E402


def clean(conn, delete: bool) -> list[int]:
    """Return the empty subject ids; delete them too if delete=True."""
    ids = store.empty_profile_ids(conn)
    if delete:
        for pid in ids:
            store.delete_profile(conn, pid)
    return ids


def main():
    delete = "--delete" in sys.argv[1:]
    conn = store.connect(config.DB_PATH)
    ids = store.empty_profile_ids(conn)
    if not ids:
        print("No empty subjects found.")
        return
    for pid in ids:
        print(f"  #{pid}  created {store.get_profile(conn, pid)['created_at']}")
    if not delete:
        print(f"{len(ids)} empty subject(s). Run again with --delete to remove them.")
        return
    backup = store.backup_database(conn, Path(config.DB_PATH))
    print(f"Backup: {backup}")
    clean(conn, delete=True)
    print(f"Deleted {len(ids)} empty subject(s).")


if __name__ == "__main__":
    main()
