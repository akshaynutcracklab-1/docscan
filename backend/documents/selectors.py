"""Read-side queries for documents."""
from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db import connection
from django.db.models import F, Q, QuerySet

from .models import Document


def documents_for(user) -> QuerySet[Document]:
    return Document.objects.filter(owner=user).select_related("document_type")


def search_documents(queryset: QuerySet[Document], q: str) -> QuerySet[Document]:
    q = (q or "").strip()
    if not q:
        return queryset
    if connection.vendor == "postgresql":
        query = SearchQuery(q, search_type="websearch", config="simple")
        return (
            queryset.filter(search_vector=query)
            .annotate(rank=SearchRank(F("search_vector"), query))
            .order_by("-rank", "-created_at")
        )
    # SQLite fallback for local development.
    return queryset.filter(Q(title__icontains=q) | Q(full_text__icontains=q))
