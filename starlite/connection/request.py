from typing import TYPE_CHECKING, Any, AsyncGenerator, Dict, Generic, Tuple, cast
from urllib.parse import parse_qsl

from orjson import loads
from starlette.requests import SERVER_PUSH_HEADERS_TO_COPY
from starlite_multipart import MultipartFormDataParser
from starlite_multipart import UploadFile as MultipartUploadFile
from starlite_multipart import parse_options_header

from starlite.connection.base import (
    ASGIConnection,
    Auth,
    User,
    empty_receive,
    empty_send,
)
from starlite.datastructures import FormMultiDict, UploadFile
from starlite.enums import RequestEncodingType
from starlite.exceptions import InternalServerException
from starlite.types import Empty

if TYPE_CHECKING:
    from typing import BinaryIO

    from starlite.handlers.http import HTTPRouteHandler  # noqa: F401
    from starlite.types.asgi_types import HTTPScope, Method, Receive, Scope, Send


class Request(Generic[User, Auth], ASGIConnection["HTTPRouteHandler", User, Auth]):
    __slots__ = ("_json", "_form", "_body", "is_connected")

    scope: "HTTPScope"

    def __init__(self, scope: "Scope", receive: "Receive" = empty_receive, send: "Send" = empty_send) -> None:
        """The Starlite Request class.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive function.
            send: The ASGI send function.
        """

        super().__init__(scope, receive, send)
        self.is_connected: bool = True
        self._body: Any = scope.get("_body", Empty)
        self._form: Any = scope.get("_form", Empty)
        self._json: Any = scope.get("_json", Empty)

    @property
    def method(self) -> "Method":
        """
        Returns:
            The request [Method][starlite.types.Method]
        """
        return self.scope["method"]

    @property
    def content_type(self) -> Tuple[str, Dict[str, str]]:
        """Parses the request's 'Content-Type' header, returning the header
        value and any options as a dictionary.

        Returns:
            A tuple with the parsed value and a dictionary containing any options send in it.
        """
        return parse_options_header(self.headers.get("Content-Type"))

    async def json(self) -> Any:
        """Method to retrieve the json request body from the request.

        This method overrides the Starlette method using the much faster orjson.loads() function

        Returns:
            An arbitrary value
        """
        if self._json is Empty:
            self._json = self.scope["_json"] = loads((await self.body()) or b"null")  # type: ignore
        return self._json

    async def stream(self) -> AsyncGenerator[bytes, None]:
        """Returns an async generator that streams chunks of bytes.

        Returns:
            An async generator.

        Raises:
            RuntimeError: if the stream is already consumed
        """
        if isinstance(self._body, bytes):
            yield self._body
            yield b""
            return

        if not self.is_connected:
            raise InternalServerException("stream consumed")

        while self.is_connected:
            event = await self.receive()
            if event["type"] == "http.request":
                body = event.get("body", b"")
                if body:
                    yield body
                if not event.get("more_body", False):
                    break
            if event["type"] == "http.disconnect":
                raise InternalServerException("client disconnected prematurely")

        self.is_connected = False
        yield b""

    async def body(self) -> bytes:
        """
        Returns:
            A byte-string representing the body of the request.
        """
        if self._body is Empty:
            chunks = []
            async for chunk in self.stream():
                chunks.append(chunk)
            self._body = self.scope["_body"] = b"".join(chunks)  # type: ignore
        return cast("bytes", self._body)

    async def form(self) -> FormMultiDict:
        """Method to retrieve form data from the request. If the request is
        either a 'multipart/form-data' or an 'application/x-www-form-
        urlencoded', this method will return a FormData instance populated with
        the values sent in the request. Otherwise, an empty instance is
        returned.

        Returns:
            A FormData instance.
        """
        if self._form is Empty:
            content_type, options = self.content_type
            if content_type == RequestEncodingType.MULTI_PART:
                parser = MultipartFormDataParser(headers=self.headers, stream=self.stream(), max_file_size=None)
                form_values = await parser()
                form_values = [
                    (
                        k,
                        UploadFile(
                            filename=v.filename,
                            content_type=v.content_type,
                            headers=v.headers,
                            file=cast("BinaryIO", v.file),
                        )
                        if isinstance(v, MultipartUploadFile)
                        else v,
                    )
                    for k, v in form_values
                ]
                self._form = self.scope["_form"] = FormMultiDict(form_values)  # type: ignore

            elif content_type == RequestEncodingType.URL_ENCODED:
                self._form = self.scope["_form"] = FormMultiDict(  # type: ignore
                    parse_qsl(
                        b"".join([chunk async for chunk in self.stream()]).decode(options.get("charset", "latin-1"))
                    )
                )
            else:
                self._form = self.scope["_form"] = FormMultiDict()  # type: ignore
        return cast("FormMultiDict", self._form)

    async def send_push_promise(self, path: str) -> None:
        """Sends a push promise. This method requires the 'http.response.push'
        extension to be sent from the ASGI server.

        Args:
            path: Path to send the promise to.

        Returns:
            None
        """
        extensions: Dict[str, Dict[Any, Any]] = self.scope.get("extensions") or {}
        if "http.response.push" in extensions:
            raw_headers = []
            for name in SERVER_PUSH_HEADERS_TO_COPY:
                for value in self.headers.getlist(name):
                    raw_headers.append((name.encode("latin-1"), value.encode("latin-1")))
            await self.send({"type": "http.response.push", "path": path, "headers": raw_headers})
