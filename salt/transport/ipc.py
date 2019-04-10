# -*- coding: utf-8 -*-
'''
IPC transport classes
'''

# Import Python libs
from __future__ import absolute_import, print_function, unicode_literals
import errno
import logging
import socket
import weakref
import time
import sys

# Import 3rd-party libs
import msgpack

# Import Tornado libs
import tornado
import tornado.gen
import tornado.netutil
import tornado.concurrent
import tornado.queues
from tornado.locks import Lock
from tornado.ioloop import IOLoop, TimeoutError as TornadoTimeoutError
from tornado.iostream import IOStream
# Import Salt libs
import salt.transport.client
import salt.transport.frame
from salt.ext import six

log = logging.getLogger(__name__)


# 'tornado.concurrent.Future' doesn't support
# remove_done_callback() which we would have called
# in the timeout case. Due to this, we have this
# callback function outside of FutureWithTimeout.
def future_with_timeout_callback(future):
    if future._future_with_timeout is not None:
        future._future_with_timeout._done_callback(future)


class FutureWithTimeout(tornado.concurrent.Future):
    def __init__(self, io_loop, future, timeout):
        super(FutureWithTimeout, self).__init__()
        self.io_loop = io_loop
        self._future = future
        if timeout is not None:
            if timeout < 0.1:
                timeout = 0.1
            self._timeout_handle = self.io_loop.add_timeout(
                self.io_loop.time() + timeout, self._timeout_callback)
        else:
            self._timeout_handle = None

        if hasattr(self._future, '_future_with_timeout'):
            # Reusing a future that has previously been used.
            # Due to this, no need to call add_done_callback()
            # because we did that before.
            self._future._future_with_timeout = self
            if self._future.done():
                future_with_timeout_callback(self._future)
        else:
            self._future._future_with_timeout = self
            self._future.add_done_callback(future_with_timeout_callback)

    def _timeout_callback(self):
        self._timeout_handle = None
        # 'tornado.concurrent.Future' doesn't support
        # remove_done_callback(). So we set an attribute
        # inside the future itself to track what happens
        # when it completes.
        self._future._future_with_timeout = None
        self.set_exception(TornadoTimeoutError())

    def _done_callback(self, future):
        try:
            if self._timeout_handle is not None:
                self.io_loop.remove_timeout(self._timeout_handle)
                self._timeout_handle = None

            self.set_result(future.result())
        except Exception as exc:
            self.set_exception(exc)


class IPCExceptionProxy(object):
    def __init__(self, orig_info):
        self.orig_info = orig_info


class IPCServer(object):
    '''
    A Tornado IPC server very similar to Tornado's TCPServer class
    but using either UNIX domain sockets or TCP sockets
    '''
    def __init__(self, socket_path, io_loop=None, payload_handler=None):
        '''
        Create a new Tornado IPC server

        :param str/int socket_path: Path on the filesystem for the
                                    socket to bind to. This socket does
                                    not need to exist prior to calling
                                    this method, but parent directories
                                    should.
                                    It may also be of type 'int', in
                                    which case it is used as the port
                                    for a tcp localhost connection.
        :param IOLoop io_loop: A Tornado ioloop to handle scheduling
        :param func payload_handler: A function to customize handling of
                                     incoming data.
        '''
        self.socket_path = socket_path
        self._started = False
        self.payload_handler = payload_handler

        # Placeholders for attributes to be populated by method calls
        self.sock = None
        self.io_loop = io_loop or IOLoop.current()
        self._closing = False

    def start(self):
        '''
        Perform the work necessary to start up a Tornado IPC server

        Blocks until socket is established
        '''
        # Start up the ioloop
        log.trace('IPCServer: binding to socket: %s', self.socket_path)
        if isinstance(self.socket_path, int):
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.setblocking(0)
            self.sock.bind(('127.0.0.1', self.socket_path))
            # Based on default used in tornado.netutil.bind_sockets()
            self.sock.listen(128)
        else:
            self.sock = tornado.netutil.bind_unix_socket(self.socket_path)

        with salt.utils.asynchronous.current_ioloop(self.io_loop):
            tornado.netutil.add_accept_handler(
                self.sock,
                self.handle_connection,
            )
        self._started = True

    @tornado.gen.coroutine
    def handle_stream(self, stream):
        '''
        Override this to handle the streams as they arrive

        :param IOStream stream: An IOStream for processing

        See https://tornado.readthedocs.io/en/latest/iostream.html#tornado.iostream.IOStream
        for additional details.
        '''
        @tornado.gen.coroutine
        def _null(msg):
            raise tornado.gen.Return(None)

        def write_callback(stream, header):
            if header.get('mid'):
                @tornado.gen.coroutine
                def return_message(msg):
                    pack = salt.transport.frame.frame_msg_ipc(
                        msg,
                        header={'mid': header['mid']},
                        raw_body=True,
                    )
                    yield stream.write(pack)
                return return_message
            else:
                return _null
        if six.PY2:
            encoding = None
        else:
            encoding = 'utf-8'
        unpacker = msgpack.Unpacker(encoding=encoding)
        while not stream.closed():
            try:
                wire_bytes = yield stream.read_bytes(4096, partial=True)
                unpacker.feed(wire_bytes)
                for framed_msg in unpacker:
                    body = framed_msg['body']
                    self.io_loop.spawn_callback(self.payload_handler, body, write_callback(stream, framed_msg['head']))
            except tornado.iostream.StreamClosedError:
                log.trace('Client disconnected from IPC %s', self.socket_path)
                break
            except socket.error as exc:
                # On occasion an exception will occur with
                # an error code of 0, it's a spurious exception.
                if exc.errno == 0:
                    log.trace('Exception occured with error number 0, '
                              'spurious exception: %s', exc)
                else:
                    log.error('Exception occurred while '
                              'handling stream: %s', exc)
            except Exception as exc:
                log.error('Exception occurred while '
                          'handling stream: %s', exc)

    def handle_connection(self, connection, address):
        log.trace('IPCServer: Handling connection '
                  'to address: %s', address)
        try:
            with salt.utils.asynchronous.current_ioloop(self.io_loop):
                stream = IOStream(
                    connection,
                )
            self.io_loop.spawn_callback(self.handle_stream, stream)
        except Exception as exc:
            log.error('IPC streaming error: %s', exc)

    def close(self):
        '''
        Routines to handle any cleanup before the instance shuts down.
        Sockets and filehandles should be closed explicitly, to prevent
        leaks.
        '''
        if self._closing:
            return
        self._closing = True
        if hasattr(self.sock, 'close'):
            self.sock.close()

    def __del__(self):
        try:
            self.close()
        except TypeError:
            # This is raised when Python's GC has collected objects which
            # would be needed when calling self.close()
            pass


class IPCClient(object):
    '''
    A Tornado IPC client very similar to Tornado's TCPClient class
    but using either UNIX domain sockets or TCP sockets

    This was written because Tornado does not have its own IPC
    server/client implementation.

    :param IOLoop io_loop: A Tornado ioloop to handle scheduling
    :param str/int socket_path: A path on the filesystem where a socket
                                belonging to a running IPCServer can be
                                found.
                                It may also be of type 'int', in which
                                case it is used as the port for a tcp
                                localhost connection.
    '''
    def __init__(self, socket_path, io_loop=None):
        '''
        Create a new IPC client

        IPC clients cannot bind to ports, but must connect to
        existing IPC servers. Clients can then send messages
        to the server.

        '''
        self.io_loop = io_loop or tornado.ioloop.IOLoop.current()
        self.socket_path = socket_path
        self._closing = False
        self.stream = None
        if six.PY2:
            encoding = None
        else:
            encoding = 'utf-8'
        self.unpacker = msgpack.Unpacker(encoding=encoding)

    def connected(self):
        return self.stream is not None and not self.stream.closed()

    def connect(self, callback=None, timeout=None):
        '''
        Connect to the IPC socket
        '''
        if hasattr(self, '_connecting_future') and not self._connecting_future.done():  # pylint: disable=E0203
            future = self._connecting_future  # pylint: disable=E0203
        else:
            if hasattr(self, '_connecting_future'):
                # read previous future result to prevent the "unhandled future exception" error
                self._connecting_future.exception()  # pylint: disable=E0203
            future = tornado.concurrent.Future()
            self._connecting_future = future
            self._connect(timeout=timeout)

        if callback is not None:
            def handle_future(future):
                response = future.result()
                self.io_loop.add_callback(callback, response)
            future.add_done_callback(handle_future)

        return future

    @tornado.gen.coroutine
    def _connect(self, timeout=None):
        '''
        Connect to a running IPCServer
        '''
        if isinstance(self.socket_path, int):
            sock_type = socket.AF_INET
            sock_addr = ('127.0.0.1', self.socket_path)
        else:
            sock_type = socket.AF_UNIX
            sock_addr = self.socket_path

        self.stream = None
        if timeout is not None:
            timeout_at = time.time() + timeout

        while True:
            if self._closing:
                break

            if self.stream is None:
                with salt.utils.asynchronous.current_ioloop(self.io_loop):
                    self.stream = IOStream(
                        socket.socket(sock_type, socket.SOCK_STREAM),
                    )

            try:
                log.trace('IPCClient: Connecting to socket: %s', self.socket_path)
                yield self.stream.connect(sock_addr)
                self._connecting_future.set_result(True)
                break
            except Exception as e:
                if self.stream.closed():
                    self.stream = None

                if timeout is None or time.time() > timeout_at:
                    if self.stream is not None:
                        self.stream.close()
                        self.stream = None
                    self._connecting_future.set_exception(e)
                    break

                yield tornado.gen.sleep(1)

    def __del__(self):
        try:
            self.close()
        except socket.error as exc:
            if exc.errno != errno.EBADF:
                # If its not a bad file descriptor error, raise
                raise
        except TypeError:
            # This is raised when Python's GC has collected objects which
            # would be needed when calling self.close()
            pass

    def close(self):
        '''
        Routines to handle any cleanup before the instance shuts down.
        Sockets and filehandles should be closed explicitly, to prevent
        leaks.
        '''
        if self._closing:
            return

        if self._refcount > 1:
            # Decrease refcount
            with self._refcount_lock:
                self._refcount -= 1
            log.debug(
                'This is not the last %s instance. Not closing yet.',
                self.__class__.__name__
            )
            return

        self._closing = True

        log.debug('Closing %s instance', self.__class__.__name__)

        if self.stream is not None and not self.stream.closed():
            self.stream.close()


class IPCMessageClient(IPCClient):
    '''
    Salt IPC message client

    Create an IPC client to send messages to an IPC server

    An example of a very simple IPCMessageClient connecting to an IPCServer. This
    example assumes an already running IPCMessage server.

    IMPORTANT: The below example also assumes a running IOLoop process.

    # Import Tornado libs
    import tornado.ioloop

    # Import Salt libs
    import salt.config
    import salt.transport.ipc

    io_loop = tornado.ioloop.IOLoop.current()

    ipc_server_socket_path = '/var/run/ipc_server.ipc'

    ipc_client = salt.transport.ipc.IPCMessageClient(ipc_server_socket_path, io_loop=io_loop)

    # Connect to the server
    ipc_client.connect()

    # Send some data
    ipc_client.send('Hello world')
    '''
    # FIXME timeout unimplemented
    # FIXME tries unimplemented
    @tornado.gen.coroutine
    def send(self, msg, timeout=None, tries=None):
        '''
        Send a message to an IPC socket

        If the socket is not currently connected, a connection will be established.

        :param dict msg: The message to be sent
        :param int timeout: Timeout when sending message (Currently unimplemented)
        '''
        if not self.connected():
            yield self.connect()
        pack = salt.transport.frame.frame_msg_ipc(msg, raw_body=True)
        yield self.stream.write(pack)


class IPCMessageServer(IPCServer):
    '''
    Salt IPC message server

    Creates a message server which can create and bind to a socket on a given
    path and then respond to messages asynchronously.

    An example of a very simple IPCServer which prints received messages to
    a console:

        # Import Tornado libs
        import tornado.ioloop

        # Import Salt libs
        import salt.transport.ipc
        import salt.config

        opts = salt.config.master_opts()

        io_loop = tornado.ioloop.IOLoop.current()
        ipc_server_socket_path = '/var/run/ipc_server.ipc'
        ipc_server = salt.transport.ipc.IPCMessageServer(opts, io_loop=io_loop
                                                         stream_handler=print_to_console)
        # Bind to the socket and prepare to run
        ipc_server.start(ipc_server_socket_path)

        # Start the server
        io_loop.start()

        # This callback is run whenever a message is received
        def print_to_console(payload):
            print(payload)

    See IPCMessageClient() for an example of sending messages to an IPCMessageServer instance
    '''


class IPCMessagePublisher(object):
    '''
    A Tornado IPC Publisher similar to Tornado's TCPServer class
    but using either UNIX domain sockets or TCP sockets
    '''
    def __init__(self, opts, socket_path, io_loop=None):
        '''
        Create a new Tornado IPC server
        :param dict opts: Salt options
        :param str/int socket_path: Path on the filesystem for the
                                    socket to bind to. This socket does
                                    not need to exist prior to calling
                                    this method, but parent directories
                                    should.
                                    It may also be of type 'int', in
                                    which case it is used as the port
                                    for a tcp localhost connection.
        :param IOLoop io_loop: A Tornado ioloop to handle scheduling
        '''
        self.opts = opts
        self.socket_path = socket_path
        self._started = False

        # Placeholders for attributes to be populated by method calls
        self.sock = None
        self.io_loop = io_loop or IOLoop.current()
        self._closing = False
        self.streams = set()

    def start(self):
        '''
        Perform the work necessary to start up a Tornado IPC server

        Blocks until socket is established
        '''
        # Start up the ioloop
        log.trace('IPCMessagePublisher: binding to socket: %s', self.socket_path)
        if isinstance(self.socket_path, int):
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.setblocking(0)
            self.sock.bind(('127.0.0.1', self.socket_path))
            # Based on default used in tornado.netutil.bind_sockets()
            self.sock.listen(128)
        else:
            self.sock = tornado.netutil.bind_unix_socket(self.socket_path)

        with salt.utils.asynchronous.current_ioloop(self.io_loop):
            tornado.netutil.add_accept_handler(
                self.sock,
                self.handle_connection,
            )
        self._started = True

    @tornado.gen.coroutine
    def _write(self, stream, pack):
        try:
            yield stream.write(pack)
        except tornado.iostream.StreamClosedError:
            log.trace('Client disconnected from IPC %s', self.socket_path)
            self.streams.discard(stream)
        except Exception as exc:
            log.error('Exception occurred while handling stream: %s', exc)
            if not stream.closed():
                stream.close()
            self.streams.discard(stream)

    def publish(self, msg):
        '''
        Send message to all connected sockets
        '''
        if not len(self.streams):
            return

        pack = salt.transport.frame.frame_msg_ipc(msg, raw_body=True)

        for stream in self.streams:
            self.io_loop.spawn_callback(self._write, stream, pack)

    def handle_connection(self, connection, address):
        log.trace('IPCServer: Handling connection to address: %s', address)
        try:
            kwargs = {}
            if self.opts['ipc_write_buffer'] > 0:
                kwargs['max_write_buffer_size'] = self.opts['ipc_write_buffer']
                log.trace('Setting IPC connection write buffer: %s', (self.opts['ipc_write_buffer']))
            with salt.utils.asynchronous.current_ioloop(self.io_loop):
                stream = IOStream(
                    connection,
                    **kwargs
                )
            self.streams.add(stream)

            def discard_after_closed():
                self.streams.discard(stream)

            stream.set_close_callback(discard_after_closed)
        except Exception as exc:
            log.error('IPC streaming error: %s', exc)

    def close(self):
        '''
        Routines to handle any cleanup before the instance shuts down.
        Sockets and filehandles should be closed explicitly, to prevent
        leaks.
        '''
        if self._closing:
            return
        self._closing = True
        for stream in self.streams:
            stream.close()
        self.streams.clear()
        if hasattr(self.sock, 'close'):
            self.sock.close()

    def __del__(self):
        try:
            self.close()
        except TypeError:
            # This is raised when Python's GC has collected objects which
            # would be needed when calling self.close()
            pass


class IPCMessageSubscriberService(IPCClient):
    '''
    IPC message subscriber service that is a standalone singleton class starting once for a number
    of IPCMessageSubscriber instances feeding all of them with data. It closes automatically when
    there are no more subscribers.

    To use this refer to IPCMessageSubscriber documentation.
    '''
    def __init__(self, socket_path, io_loop=None):
        super(IPCMessageSubscriberService, self).__init__(
            socket_path, io_loop=io_loop)
        self.saved_data = []
        self._read_in_progress = Lock()
        self.handlers = weakref.WeakSet()
        self.read_stream_future = None

    def _subscribe(self, handler):
        self.handlers.add(handler)

    def unsubscribe(self, handler):
        self.handlers.discard(handler)

    def _has_subscribers(self):
        return bool(self.handlers)

    def _feed_subscribers(self, data):
        for subscriber in self.handlers:
            subscriber._feed(data)

    @tornado.gen.coroutine
    def _read(self, timeout, callback=None):
        try:
            yield self._read_in_progress.acquire(timeout=0)
        except tornado.gen.TimeoutError:
            raise tornado.gen.Return(None)

        log.debug('IPC Subscriber Service is starting reading')
        # If timeout is not specified we need to set some here to make the service able to check
        # is there any handler waiting for data.
        if timeout is None:
            timeout = 5

        self.read_stream_future = None
        while self._has_subscribers():
            if self.read_stream_future is None:
                self.read_stream_future = self.stream.read_bytes(4096, partial=True)

            try:
                wire_bytes = yield FutureWithTimeout(self.io_loop,
                                                     self.read_stream_future,
                                                     timeout)
                self.read_stream_future = None

                self.unpacker.feed(wire_bytes)
                msgs = [msg['body'] for msg in self.unpacker]
                self._feed_subscribers(msgs)
            except TornadoTimeoutError:
                # Continue checking are there alive waiting handlers
                # Keep 'read_stream_future' alive to wait it more in the next loop
                continue
            except tornado.iostream.StreamClosedError as exc:
                log.trace('Subscriber disconnected from IPC %s', self.socket_path)
                self._feed_subscribers([None])
                break
            except Exception as exc:
                log.error('Exception occurred in Subscriber while handling stream: %s', exc)
                exc = IPCExceptionProxy(sys.exc_info())
                self._feed_subscribers([exc])
                break

        log.debug('IPC Subscriber Service is stopping due to a lack of subscribers')
        self._read_in_progress.release()
        raise tornado.gen.Return(None)

    @tornado.gen.coroutine
    def read(self, handler, timeout=None):
        '''
        Asynchronously read messages and invoke a callback when they are ready.

        :param callback: A callback with the received data
        '''
        self._subscribe(handler)
        while not self.connected():
            try:
                yield self.connect(timeout=5)
            except tornado.iostream.StreamClosedError:
                log.trace('Subscriber closed stream on IPC %s before connect', self.socket_path)
                yield tornado.gen.sleep(1)
            except Exception as exc:
                log.error('Exception occurred while Subscriber connecting: %s', exc)
                yield tornado.gen.sleep(1)
        yield self._read(timeout)

    def close(self):
        '''
        Routines to handle any cleanup before the instance shuts down.
        Sockets and filehandles should be closed explicitly, to prevent
        leaks.
        '''
        super(IPCMessageSubscriberService, self).close()
        if self.read_stream_future is not None and self.read_stream_future.done():
            exc = self.read_stream_future.exception()
            if exc and not isinstance(exc, tornado.iostream.StreamClosedError):
                log.error("Read future returned exception %r", exc)

    def __del__(self):
        if IPCMessageSubscriberService in globals():
            self.close()


class IPCMessageSubscriber(object):
    '''
    Salt IPC message subscriber

    Create or reuse an IPC client to receive messages from IPC publisher

    An example of a very simple IPCMessageSubscriber connecting to an IPCMessagePublisher.
    This example assumes an already running IPCMessagePublisher.

    IMPORTANT: The below example also assumes the IOLoop is NOT running.

    # Import Tornado libs
    import tornado.ioloop

    # Import Salt libs
    import salt.config
    import salt.transport.ipc

    # Create a new IO Loop.
    # We know that this new IO Loop is not currently running.
    io_loop = tornado.ioloop.IOLoop()

    ipc_publisher_socket_path = '/var/run/ipc_publisher.ipc'

    ipc_subscriber = salt.transport.ipc.IPCMessageSubscriber(ipc_server_socket_path, io_loop=io_loop)

    # Connect to the server
    # Use the associated IO Loop that isn't running.
    io_loop.run_sync(ipc_subscriber.connect)

    # Wait for some data
    package = ipc_subscriber.read_sync()
    '''
    def __init__(self, socket_path, io_loop=None):
        self.service = IPCMessageSubscriberService(socket_path, io_loop)
        self.queue = tornado.queues.Queue()

    def connected(self):
        return self.service.connected()

    def connect(self, callback=None, timeout=None):
        return self.service.connect(callback=callback, timeout=timeout)

    @tornado.gen.coroutine
    def _feed(self, msgs):
        for msg in msgs:
            yield self.queue.put(msg)

    @tornado.gen.coroutine
    def read_async(self, callback, timeout=None):
        '''
        Asynchronously read messages and invoke a callback when they are ready.

        :param callback: A callback with the received data
        '''
        self.service.read(self)
        while True:
            try:
                if timeout is not None:
                    deadline = time.time() + timeout
                else:
                    deadline = None
                data = yield self.queue.get(timeout=deadline)
            except tornado.gen.TimeoutError:
                raise tornado.gen.Return(None)
            if data is None:
                break
            elif isinstance(data, IPCExceptionProxy):
                six.reraise(*data.orig_info)
            elif callback:
                self.service.io_loop.spawn_callback(callback, data)
            else:
                raise tornado.gen.Return(data)

    def read_sync(self, timeout=None):
        '''
        Read a message from an IPC socket

        The associated IO Loop must NOT be running.
        :param int timeout: Timeout when receiving message
        :return: message data if successful. None if timed out. Will raise an
                 exception for all other error conditions.
        '''
        return self.service.io_loop.run_sync(lambda: self.read_async(None, timeout))

    def close(self):
        self.service.unsubscribe(self)

    def __del__(self):
        self.close()
