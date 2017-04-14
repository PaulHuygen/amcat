
        
from rest_framework.views import APIView
from rest_framework import status
from rest_framework import parsers
from rest_framework import renderers
from rest_framework.response import Response
from rest_framework.authtoken.models import Token
from django.contrib.auth import authenticate
from rest_framework import serializers
import datetime

from amcat._version import __version__

class AuthTokenSerializer(serializers.Serializer):
    def __init__(self, *args, **kwargs):
        super(AuthTokenSerializer, self).__init__(*args, **kwargs)
        self.user = None

    username = serializers.CharField()
    password = serializers.CharField()

    def validate(self, attrs):
        username = attrs.get('username')
        password = attrs.get('password')

        if username and password:
            self.user = authenticate(username=username, password=password)

            if self.user:
                if not self.user.is_active:
                    raise serializers.ValidationError('User account is disabled.')
                return attrs
            else:
                raise serializers.ValidationError('Unable to login with provided credentials.')
        else:
            raise serializers.ValidationError('Must include "username" and "password"')



class ObtainAuthToken(APIView):
    throttle_classes = ()
    permission_classes = ()
    parser_classes = (parsers.FormParser, parsers.MultiPartParser, parsers.JSONParser,)
    renderer_classes = (renderers.JSONRenderer,)
    serializer_class = AuthTokenSerializer
    model = Token

    def post(self, request):
        serializer = self.serializer_class(data=request.DATA)
        if serializer.is_valid():
            token = get_token(serializer.user)
            return Response({'token': token.key, 'version': __version__})
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

def get_token(user):
    token, created = Token.objects.get_or_create(user=user)
    if not created:
        token.created = datetime.datetime.now()
        token.save()
    return token


obtain_auth_token = ObtainAuthToken.as_view()
