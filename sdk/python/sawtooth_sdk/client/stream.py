# Copyright 2016 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ------------------------------------------------------------------------------

import asyncio
import hashlib
import logging
import random
import string
from threading import Event
from threading import Thread
from threading import Condition

import zmq
import zmq.asyncio

import sawtooth_sdk.protobuf.validator_pb2 as validator_pb2

from sawtooth_sdk.client.exceptions import ValidatorConnectionError
from sawtooth_sdk.client.future import Future
from sawtooth_sdk.client.future import FutureCollection
from sawtooth_sdk.client.future import FutureCollectionKeyError
from sawtooth_sdk.client.future import FutureResult
from sawtooth_sdk.client.future import FutureError

LOGGER = logging.getLogger(__file__)

# Used to send a message to core.TransactionProcessor to reregister with
# the validator and wait for TP_PROCESS_REQUEST. All other items on the
# queue will be validator_pb2.Message objects, so -1 will be an exceptional
# event.
RECONNECT_EVENT = -1


def _generate_id():
    return hashlib.sha512(''.join(
        [random.choice(string.ascii_letters)
            for _ in range(0, 1024)]).encode('ascii')).hexdigest()


class _SendReceiveThread(Thread):
    """
    Internal thread to Stream class that runs the asyncio event loop.
    """

    def __init__(self, url, futures, ready_event):
        """constructor for background thread

        :param url (str): the address to connect to the validator on
        :param futures (FutureCollection): The Futures associated with
                messages sent through Stream.send
        :param ready_event (threading.Event): used to notify waiting/asking
               classes that the background thread of Stream is ready after
               a disconnect event.
        """
        super(_SendReceiveThread, self).__init__()
        self._futures = futures
        self._url = url

        self._event_loop = None
        self._sock = None
        self._monitor_sock = None
        self._recv_queue = None
        self._send_queue = None
        self._context = None
        self._ready_event = ready_event
        self._condition = Condition()

    @asyncio.coroutine
    def _receive_message(self):
        """
        internal coroutine that receives messages and puts
        them on the recv_queue
        """
        while True:
            if not self._ready_event.is_set():
                break
            msg_bytes = yield from self._sock.recv()
            message = validator_pb2.Message()
            message.ParseFromString(msg_bytes)
            try:
                self._futures.set_result(
                    message.correlation_id,
                    FutureResult(message_type=message.message_type,
                                 content=message.content))
                self._futures.remove(message.correlation_id)
            except FutureCollectionKeyError:
                # if we are getting an initial message, not a response
                if not self._ready_event.is_set():
                    break
                self._recv_queue.put_nowait(message)

    @asyncio.coroutine
    def _send_message(self):
        """
        internal coroutine that sends messages from the send_queue
        """
        while True:
            if not self._ready_event.is_set():
                break
            msg = yield from self._send_queue.get()
            yield from self._sock.send_multipart([msg.SerializeToString()])

    @asyncio.coroutine
    def _put_message(self, message):
        """
        Puts a message on the send_queue. Not to be accessed directly.
        :param message: protobuf generated validator_pb2.Message
        """
        self._send_queue.put_nowait(message)

    @asyncio.coroutine
    def _get_message(self):
        """
        Gets a message from the recv_queue. Not to be accessed directly.
        """
        with self._condition:
            self._condition.wait_for(lambda: self._recv_queue is not None)
        msg = yield from self._recv_queue.get()

        return msg

    @asyncio.coroutine
    def _monitor_disconnects(self):
        """Monitors the client socket for disconnects
        """
        yield from self._monitor_sock.recv()
        LOGGER.debug("monitor socket received disconnect event")
        self._ready_event.clear()
        for future in self._futures.future_values():
            future.set_result(FutureError())
        for task in asyncio.Task.all_tasks(self._event_loop):
            task.cancel()
        self._send_queue = None
        self._recv_queue = None
        self._sock.close()
        self._sock = None
        self._event_loop.call_soon(self._event_loop.stop)

    def put_message(self, message):
        """
        :param message: protobuf generated validator_pb2.Message
        """
        if not self._ready_event.is_set():
            return
        with self._condition:
            self._condition.wait_for(lambda: self._event_loop is not None
                                     and self._send_queue is not None)
        asyncio.run_coroutine_threadsafe(self._put_message(message),
                                         self._event_loop)

    def get_message(self):
        """
        :return message: concurrent.futures.Future
        """
        with self._condition:
            self._condition.wait_for(lambda: self._event_loop is not None)
        return asyncio.run_coroutine_threadsafe(self._get_message(),
                                                self._event_loop)

    def _tasks_yet_to_be_done(self):
        """Gathers all the tasks (pending coroutines and futures)
        and provides a future that is done when all tasks are done.
        :return: concurrent.futures.Future
        """
        return asyncio.gather(*asyncio.Task.all_tasks(self._event_loop))

    def shutdown(self):
        """Schedules a callback to be run when all of the tasks
         have completed.
        """
        future = self._tasks_yet_to_be_done()
        future.add_done_callback(self._done_callback)

    def _done_callback(self, future):
        """Stops the event loop, closes the socket, and destroys the context

        :param future: concurrent.futures.Future not used
        """
        self._event_loop.call_soon_threadsafe(self._event_loop.stop)
        self._sock.close()
        self._context.destroy()

    def run(self):
        first_time = True
        while True:
            if self._event_loop is None:
                self._event_loop = zmq.asyncio.ZMQEventLoop()
                asyncio.set_event_loop(self._event_loop)
            if self._context is None:
                self._context = zmq.asyncio.Context()
            self._sock = self._context.socket(zmq.DEALER)
            self._sock.identity = _generate_id()[0:16].encode('ascii')
            self._sock.set(zmq.LINGER, 0)
            self._sock.connect('tcp://' + self._url)
            self._monitor_sock = self._sock.get_monitor_socket(
                zmq.EVENT_DISCONNECTED)
            self._send_queue = asyncio.Queue(loop=self._event_loop)
            self._recv_queue = asyncio.Queue(loop=self._event_loop)
            if first_time is False:
                self._recv_queue.put_nowait(RECONNECT_EVENT)
            with self._condition:
                self._condition.notify_all()
            asyncio.ensure_future(self._send_message(),
                                  loop=self._event_loop)
            asyncio.ensure_future(self._receive_message(),
                                  loop=self._event_loop)
            asyncio.ensure_future(self._monitor_disconnects(),
                                  loop=self._event_loop)

            self._ready_event.set()
            self._event_loop.run_forever()
            if first_time is True:
                first_time = False


class Stream(object):
    def __init__(self, url):
        self._url = url
        self._futures = FutureCollection()
        self._event = Event()
        self._event.set()
        self._send_recieve_thread = _SendReceiveThread(
            url,
            futures=self._futures,
            ready_event=self._event)
        self._send_recieve_thread.daemon = True
        self._send_recieve_thread.start()

    def send(self, message_type, content):
        """Send a message to the validator

        :param: message_type(validator_pb2.Message.MessageType)
        :param: content(bytes)
        :return: (future.Future)
        :raises: (ValidatorConnectionError)
        """

        if not self._event.is_set():
            raise ValidatorConnectionError()
        message = validator_pb2.Message(
            message_type=message_type,
            correlation_id=_generate_id(),
            content=content)
        future = Future(message.correlation_id)
        self._futures.put(future)

        self._send_recieve_thread.put_message(message)
        return future

    def send_back(self, message_type, correlation_id, content):
        """
        Return a response to a message.
        :param message_type: validator_pb2.Message.MessageType enum value
        :param correlation_id: a random str internal to the validator
        :param content: protobuf bytes
        :raises (ValidatorConnectionError):
        """
        if not self._event.is_set():
            raise ValidatorConnectionError()
        message = validator_pb2.Message(
            message_type=message_type,
            correlation_id=correlation_id,
            content=content)
        self._send_recieve_thread.put_message(message)

    def receive(self):
        """
        Receive messages that are not responses
        :return: concurrent.futures.Future
        """
        return self._send_recieve_thread.get_message()

    def wait_for_ready(self):
        """Blocks until the background thread has recovered
        from a disconnect with the validator.
        """
        self._event.wait()

    def is_ready(self):
        """Whether the background thread has recovered from
        a disconnect with the validator

        :return: (bool) whether the background thread is ready
        """
        return self._event.is_set()

    def close(self):
        self._send_recieve_thread.shutdown()
