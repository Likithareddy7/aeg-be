"""The hybrid memory layer from the target architecture.

  exact store     PostgreSQL / Cloud SQL (MemoryRecord) -- by id, module, error code
  semantic store  pgvector (default) or Vertex AI Vector Search
  keyword store   PostgreSQL full-text (default) or Firestore BM25

What goes in: test cases, failed runs, approved self-heals and filed defects,
written by the learning loop (learning.py) as they change.
Who reads it: the test generation agent's `search_project_memory` tool and
GET /api/projects/{id}/memory.

Backends are chosen with MEMORY_SEMANTIC_BACKEND / MEMORY_KEYWORD_BACKEND
(see config.py); the defaults need nothing but PostgreSQL with pgvector.
"""

from .service import KINDS, MemoryHit, MemoryItem, MemoryService, build_memory_service

__all__ = ["KINDS", "MemoryHit", "MemoryItem", "MemoryService", "build_memory_service"]
