from rest_framework.permissions import BasePermission


class IsDocumentOwner(BasePermission):
    """Object-level guard. Querysets are already scoped to the user, so others get 404;
    this is defence in depth for any view that forgets to scope its queryset."""

    def has_object_permission(self, request, view, obj):
        document = getattr(obj, "document", obj)
        return document.owner_id == request.user.id
