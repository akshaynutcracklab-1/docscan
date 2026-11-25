import django_filters

from .models import Document
from .selectors import search_documents


class DocumentFilter(django_filters.FilterSet):
    type = django_filters.CharFilter(field_name="document_type__code", label="Document type code")
    status = django_filters.ChoiceFilter(choices=Document.Status.choices)
    q = django_filters.CharFilter(method="filter_q", label="Full-text search on title and text")

    class Meta:
        model = Document
        fields = ("type", "status", "q")

    def filter_q(self, queryset, name, value):
        return search_documents(queryset, value)
