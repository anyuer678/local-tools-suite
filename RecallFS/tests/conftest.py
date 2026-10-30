from __future__ import annotations

import pytest

from recallfs.fulltext_index import IndexDatabase
from recallfs.index_scheduler import IndexScheduler
from recallfs.project_collections import CollectionStore, FavoritesStore, RecentSearchStore
from recallfs.search_engine import SearchEngine
from recallfs.semantic_index import SemanticIndex


@pytest.fixture
def corpus(tmp_path):
    path = tmp_path / "corpus"
    path.mkdir()
    return path


@pytest.fixture
def db(tmp_path):
    database = IndexDatabase(tmp_path / "index" / "recallfs.sqlite3")
    yield database
    database.close()


@pytest.fixture
def scheduler(db):
    return IndexScheduler(db)


@pytest.fixture
def engine(db):
    collections = CollectionStore(db)
    recent = RecentSearchStore(db)
    return SearchEngine(db, semantic=SemanticIndex(db), collections=collections, recent=recent)


@pytest.fixture
def stores(db):
    return {
        "collections": CollectionStore(db),
        "favorites": FavoritesStore(db),
        "recent": RecentSearchStore(db),
    }
