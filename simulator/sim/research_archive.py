"""Private immutable research snapshots, loaded only when an evaluator run starts."""
from pathlib import Path
import re

from contracts.research import LiveCollection
from sim.research_query import EvidenceQuery, query_evidence

ARCHIVE_DIR = Path(__file__).resolve().parent.parent / "research_archive"


class ResearchArchive:
    def __init__(self, root: Path = ARCHIVE_DIR):
        self.root = root

    def save(self, collection: LiveCollection) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / f"{collection.collection_id}.json").open("x") as fh:
            fh.write(collection.model_dump_json(indent=2))

    def load(self, collection_id: str) -> LiveCollection:
        if not re.fullmatch(r"research_[0-9a-f]{32}", collection_id):
            raise ValueError("invalid collection id")
        try:
            collection = LiveCollection.model_validate_json((self.root / f"{collection_id}.json").read_text())
        except FileNotFoundError:
            raise KeyError("unknown research collection") from None
        if collection.collection_id != collection_id:
            raise ValueError("collection id mismatch")
        return collection

    def sources(self, query: EvidenceQuery, collection_id: str | None = None) -> dict:
        # Immutable JSON collections remain the source of truth. No index can go stale.
        collections = [self.load(collection_id)] if collection_id else [
            self.load(path.stem) for path in sorted(self.root.glob("research_*.json"))]
        rows = [{"source": source, "collection_id": batch.collection_id}
                for batch in collections for source in batch.sources]
        return query_evidence(rows, query)
