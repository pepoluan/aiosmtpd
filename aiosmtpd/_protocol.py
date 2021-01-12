"""Stream-related things."""

__all__ = [
    "StreamReader",
    "StreamWriter",
    "StreamReaderProtocol",
    "open_connection",
    "start_server",
    "IncompleteReadError",
    "LimitOverrunError",
]

import socket
import logging

if hasattr(socket, "AF_UNIX"):
    __all__.extend(["open_unix_connection", "start_unix_server"])

from asyncio import coroutines, events, protocols
from asyncio.streams import StreamReader, StreamWriter
from asyncio.coroutines import coroutine


_DEFAULT_LIMIT = 2 ** 16
logger = logging.getLogger()


class IncompleteReadError(EOFError):
    """
    Incomplete read error. Attributes:

    - partial: read bytes string before the end of stream was reached
    - expected: total number of expected bytes (or None if unknown)
    """

    def __init__(self, partial, expected):
        super().__init__(
            "%d bytes read on a total of %r expected bytes" % (len(partial), expected)
        )
        self.partial = partial
        self.expected = expected

    def __reduce__(self):
        return type(self), (self.partial, self.expected)


class LimitOverrunError(Exception):
    """Reached the buffer limit while looking for a separator.

    Attributes:
    - consumed: total number of to be consumed bytes.
    """

    def __init__(self, message, consumed):
        super().__init__(message)
        self.consumed = consumed

    def __reduce__(self):
        return type(self), (self.args[0], self.consumed)


@coroutine
def open_connection(host=None, port=None, *, loop=None, limit=_DEFAULT_LIMIT, **kwds):
    """A wrapper for create_connection() returning a (reader, writer) pair.

    The reader returned is a StreamReader instance; the writer is a
    StreamWriter instance.

    The arguments are all the usual arguments to create_connection()
    except protocol_factory; most common are positional host and port,
    with various optional keyword arguments following.

    Additional optional keyword arguments are loop (to set the event loop
    instance to use) and limit (to set the buffer limit passed to the
    StreamReader).

    (If you want to customize the StreamReader and/or
    StreamReaderProtocol classes, just copy the code -- there's
    really nothing special here except some convenience.)
    """
    if loop is None:
        loop = events.get_event_loop()
    reader = StreamReader(limit=limit, loop=loop)
    protocol = StreamReaderProtocol(reader, loop=loop)
    transport, _ = yield from loop.create_connection(
        lambda: protocol, host, port, **kwds
    )
    writer = StreamWriter(transport, protocol, reader, loop)
    return reader, writer


@coroutine
def start_server(
    client_connected_cb,
    host=None,
    port=None,
    *,
    loop=None,
    limit=_DEFAULT_LIMIT,
    **kwds
):
    """Start a socket server, call back for each client connected.

    The first parameter, `client_connected_cb`, takes two parameters:
    client_reader, client_writer.  client_reader is a StreamReader
    object, while client_writer is a StreamWriter object.  This
    parameter can either be a plain callback function or a coroutine;
    if it is a coroutine, it will be automatically converted into a
    Task.

    The rest of the arguments are all the usual arguments to
    loop.create_server() except protocol_factory; most common are
    positional host and port, with various optional keyword arguments
    following.  The return value is the same as loop.create_server().

    Additional optional keyword arguments are loop (to set the event loop
    instance to use) and limit (to set the buffer limit passed to the
    StreamReader).

    The return value is the same as loop.create_server(), i.e. a
    Server object which can be used to stop the service.
    """
    if loop is None:
        loop = events.get_event_loop()

    def factory():
        reader = StreamReader(limit=limit, loop=loop)
        protocol = StreamReaderProtocol(reader, client_connected_cb, loop=loop)
        return protocol

    return (yield from loop.create_server(factory, host, port, **kwds))


if hasattr(socket, "AF_UNIX"):
    # UNIX Domain Sockets are supported on this platform

    @coroutine
    def open_unix_connection(path=None, *, loop=None, limit=_DEFAULT_LIMIT, **kwds):
        """Similar to `open_connection` but works with UNIX Domain Sockets."""
        if loop is None:
            loop = events.get_event_loop()
        reader = StreamReader(limit=limit, loop=loop)
        protocol = StreamReaderProtocol(reader, loop=loop)
        transport, _ = yield from loop.create_unix_connection(
            lambda: protocol, path, **kwds
        )
        writer = StreamWriter(transport, protocol, reader, loop)
        return reader, writer

    @coroutine
    def start_unix_server(
        client_connected_cb, path=None, *, loop=None, limit=_DEFAULT_LIMIT, **kwds
    ):
        """Similar to `start_server` but works with UNIX Domain Sockets."""
        if loop is None:
            loop = events.get_event_loop()

        def factory():
            reader = StreamReader(limit=limit, loop=loop)
            protocol = StreamReaderProtocol(reader, client_connected_cb, loop=loop)
            return protocol

        return (yield from loop.create_unix_server(factory, path, **kwds))


class FlowControlMixin(protocols.Protocol):
    """Reusable flow control logic for StreamWriter.drain().

    This implements the protocol methods pause_writing(),
    resume_reading() and connection_lost().  If the subclass overrides
    these it must call the super methods.

    StreamWriter.drain() must wait for _drain_helper() coroutine.
    """

    def __init__(self, loop=None):
        if loop is None:
            self._loop = events.get_event_loop()
        else:
            self._loop = loop
        self._paused = False
        self._drain_waiter = None
        self._connection_lost = False

    def pause_writing(self):
        assert not self._paused
        self._paused = True
        if self._loop.get_debug():
            logger.debug("%r pauses writing", self)

    def resume_writing(self):
        assert self._paused
        self._paused = False
        if self._loop.get_debug():
            logger.debug("%r resumes writing", self)

        waiter = self._drain_waiter
        if waiter is not None:
            self._drain_waiter = None
            if not waiter.done():
                waiter.set_result(None)

    def connection_lost(self, exc):
        self._connection_lost = True
        # Wake up the writer if currently paused.
        if not self._paused:
            return
        waiter = self._drain_waiter
        if waiter is None:
            return
        self._drain_waiter = None
        if waiter.done():
            return
        if exc is None:
            waiter.set_result(None)
        else:
            waiter.set_exception(exc)

    @coroutine
    def _drain_helper(self):
        if self._connection_lost:
            raise ConnectionResetError("Connection lost")
        if not self._paused:
            return
        waiter = self._drain_waiter
        assert waiter is None or waiter.cancelled()
        waiter = self._loop.create_future()
        self._drain_waiter = waiter
        yield from waiter


class StreamReaderProtocol(FlowControlMixin, protocols.Protocol):
    """Helper class to adapt between Protocol and StreamReader.

    (This is a helper class instead of making StreamReader itself a
    Protocol subclass, because the StreamReader has other potential
    uses, and to prevent the user of the StreamReader to accidentally
    call inappropriate methods of the protocol.)
    """

    def __init__(self, stream_reader, client_connected_cb=None, loop=None):
        super().__init__(loop=loop)
        self._stream_reader = stream_reader
        self._stream_writer = None
        self._client_connected_cb = client_connected_cb
        self._over_ssl = False

    def connection_made(self, transport):
        self._stream_reader.set_transport(transport)
        self._over_ssl = transport.get_extra_info("sslcontext") is not None
        if self._client_connected_cb is not None:
            self._stream_writer = StreamWriter(
                transport, self, self._stream_reader, self._loop
            )
            res = self._client_connected_cb(self._stream_reader, self._stream_writer)
            if coroutines.iscoroutine(res):
                self._loop.create_task(res)

    def connection_lost(self, exc):
        if self._stream_reader is not None:
            if exc is None:
                self._stream_reader.feed_eof()
            else:
                self._stream_reader.set_exception(exc)
        super().connection_lost(exc)
        self._stream_reader = None
        self._stream_writer = None

    def data_received(self, data):
        self._stream_reader.feed_data(data)

    def eof_received(self):
        self._stream_reader.feed_eof()
        if self._over_ssl:
            # Prevent a warning in SSLProtocol.eof_received:
            # "returning true from eof_received()
            # has no effect when using ssl"
            return False
        return True
