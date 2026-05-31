"""
One-off migration: copy user profiles from Firestore into the local SQLite store.

Run this ONCE on a machine that still has Firestore credentials (e.g. via
`gcloud auth application-default login`) before cutting the app over to SQLite:

    python migrate_firestore_to_sqlite.py

Requires `google-cloud-firestore` to be installed (it is in the existing venv;
it has been removed from requirements.txt since the app no longer needs it at
runtime). Honors the same env vars the old backend used:
    FIRESTORE_USERS_COLLECTION (default "users")
    FIRESTORE_DATABASE         (default "(default)")
    USER_DB_PATH               (default "users.db") — where SQLite is written
"""

import os

import user_store


def main():
    from google.cloud import firestore

    collection = os.environ.get("FIRESTORE_USERS_COLLECTION", "users")
    database = os.environ.get("FIRESTORE_DATABASE", "(default)")
    db_path = os.environ.get("USER_DB_PATH", "users.db")

    client = firestore.Client(database=database)
    count = 0
    for doc in client.collection(collection).stream():
        data = doc.to_dict() or {}
        user_store.set_user(doc.id, data)
        count += 1
        print(f"  migrated {doc.id}")

    print(f"Done. {count} profile(s) written to {db_path}")


if __name__ == "__main__":
    main()
