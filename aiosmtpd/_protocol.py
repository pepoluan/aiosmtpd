"""Stream-related things."""


import logging
from asyncio import AbstractEventLoop, BaseTransport, events, protocols
from asyncio.streams import StreamReader, StreamWriter
from typing import Optional

from public import public

logger = logging.getLogger()


@public
class FlowControlMixin(protocols.Protocol):
    """Reusable flow control logic for StreamWriter.drain().

    This implements the protocol methods pause_writing(),
    resume_writing() and connection_lost().  If the subclass overrides
    these it must call the super methods.

    StreamWriter.drain() must wait for _drain_helper() coroutine.
    """

    def __init__(self, loop: AbstractEventLoop = None):
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

    def connection_lost(self, exc: Exception):
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

    async def _drain_helper(self):
        if self._connection_lost:
            raise ConnectionResetError("Connection lost")
        if not self._paused:
            return
        waiter = self._drain_waiter
        assert waiter is None or waiter.cancelled()
        waiter = self._loop.create_future()
        self._drain_waiter = waiter
        await waiter

    def _get_close_waiter(self, stream):
        raise NotImplementedError


@public
class StreamReaderProtocol(FlowControlMixin, protocols.Protocol):
    """Helper class to adapt between Protocol and StreamReader.

    (This is a helper class instead of making StreamReader itself a
    Protocol subclass, because the StreamReader has other potential
    uses, and to prevent the user of the StreamReader to accidentally
    call inappropriate methods of the protocol.)
    """

    def __init__(self, stream_reader: StreamReader, loop: AbstractEventLoop = None):
        super().__init__(loop=loop)
        self._stream_reader: StreamReader = stream_reader
        self._stream_writer: Optional[StreamWriter] = None
        self._over_ssl: bool = False
        self._closed = self._loop.create_future()

    def connection_made(self, transport: BaseTransport):
        self._stream_reader.set_transport(transport)
        self._over_ssl = transport.get_extra_info("sslcontext") is not None
        self._stream_writer = StreamWriter(
            transport, self, self._stream_reader, self._loop
        )

    def connection_lost(self, exc: Exception):
        if self._stream_reader is not None:
            if exc is None:
                self._stream_reader.feed_eof()
            else:
                self._stream_reader.set_exception(exc)
        super().connection_lost(exc)
        self._stream_reader = None
        self._stream_writer = None

    def data_received(self, data: bytes):
        self._stream_reader.feed_data(data)

    def eof_received(self):
        self._stream_reader.feed_eof()
        if self._over_ssl:
            # Prevent a warning in SSLProtocol.eof_received:
            # "returning true from eof_received()
            # has no effect when using ssl"
            return False
        return True

    def _get_close_waiter(self, stream):
        return self._closed

    def __del__(self):
        # Prevent reports about unhandled exceptions.
        # Better than self._closed._log_traceback = False hack
        closed = getattr(self, "_closed", None)
        if closed and closed.done() and not closed.cancelled():
            closed.exception()
