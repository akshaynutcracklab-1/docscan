from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import generics, permissions, serializers

from .serializers import RegisterSerializer, UserSerializer


@extend_schema(
    responses={
        201: inline_serializer(
            "RegisterResponse",
            fields={"user": UserSerializer(), "access": serializers.CharField(), "refresh": serializers.CharField()},
        )
    }
)
class RegisterView(generics.CreateAPIView):
    """Create an account and return a JWT pair so the client is logged in immediately."""

    serializer_class = RegisterSerializer
    permission_classes = (permissions.AllowAny,)
    authentication_classes = ()


class MeView(generics.RetrieveAPIView):
    serializer_class = UserSerializer

    def get_object(self):
        return self.request.user
