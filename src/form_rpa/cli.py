from __future__ import annotations
import argparse
from .inputs import JsonFormSource
from .pipeline import FormPipeline
from .receipts import ReceiptStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--database", default="form-rpa.sqlite3")
    args = parser.parse_args()
    store = ReceiptStore(args.database)
    try:
        accepted, duplicates = FormPipeline(JsonFormSource(args.fixture), store).run()
        print({"accepted": accepted, "duplicates": duplicates, "database": args.database})
    finally:
        store.close()


if __name__ == "__main__":
    main()
