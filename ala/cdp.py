"""A private, bounded CDP pipe connected only to the Chrome child we spawn."""
from __future__ import annotations

from collections import deque
import fcntl
import json
import os
import select
import signal
import time


class CDPError(RuntimeError):
    pass


class CDPResponseTimeout(CDPError):
    """The complete command was sent, but its matching response did not arrive.

    Sending failures deliberately remain CDPError: retrying a partially sent
    JSON/NUL command would corrupt this same private pipe's framing.
    """


class PipeBrowser:
    MAX_MESSAGE = 4 * 1024 * 1024
    MAX_EVENTS = 1024

    def __init__(self, argv, *, spawn=None):
        # Reserve high descriptors first: dup2 actions must never overwrite one
        # of the source descriptors. posix_spawn avoids a threaded preexec_fn.
        descriptors = []
        self.pid = None
        self.returncode = None
        self._buffer = bytearray()
        self._events = deque()
        self._serial = 0
        try:
            for _ in range(2):
                low = os.pipe()
                try:
                    descriptors.extend(fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10) for fd in low)
                finally:
                    for fd in low:
                        os.close(fd)
            incoming, self._write, self._read, outgoing = descriptors
            actions = [(os.POSIX_SPAWN_DUP2, incoming, 3),
                       (os.POSIX_SPAWN_DUP2, outgoing, 4)]
            actions += [(os.POSIX_SPAWN_CLOSE, fd) for fd in descriptors]
            self.pid = (spawn or os.posix_spawn)(argv[0], list(argv), dict(os.environ), file_actions=actions)
            os.close(incoming)
            os.close(outgoing)
            descriptors = [self._write, self._read]
            os.set_blocking(self._read, False)
            os.set_blocking(self._write, False)
        except BaseException:
            for fd in descriptors:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise

    def poll(self):
        if self.returncode is None:
            try:
                pid, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                self.returncode = -1
            else:
                if pid:
                    self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def _receive(self, deadline):
        while True:
            end = self._buffer.find(0)
            if end >= 0:
                if end > self.MAX_MESSAGE:
                    raise CDPError('브라우저 제어 응답 크기 제한을 초과했습니다.')
                packet = bytes(self._buffer[:end])
                del self._buffer[:end + 1]
                try:
                    value = json.loads(packet)
                except (ValueError, UnicodeError):
                    raise CDPError('브라우저 제어 응답 형식이 올바르지 않습니다.') from None
                if not isinstance(value, dict):
                    raise CDPError('브라우저 제어 응답은 JSON object여야 합니다.')
                return value
            if len(self._buffer) > self.MAX_MESSAGE:
                raise CDPError('브라우저 제어 응답 크기 제한을 초과했습니다.')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if not select.select([self._read], [], [], remaining)[0]:
                return None
            try:
                block = os.read(self._read, 65536)
            except BlockingIOError:
                continue
            if not block:
                raise CDPError('브라우저 제어 pipe 연결이 종료되었습니다.')
            self._buffer.extend(block)

    def _event(self, value):
        if len(self._events) >= self.MAX_EVENTS:
            raise CDPError('브라우저 제어 이벤트 처리 한도를 초과했습니다.')
        self._events.append(value)

    def call(self, method, params=None, *, session_id=None, timeout=5):
        self._serial += 1
        value = {'id': self._serial, 'method': method, 'params': params or {}}
        if session_id is not None:
            value['sessionId'] = session_id
        packet = memoryview(json.dumps(value, separators=(',', ':')).encode() + b'\0')
        deadline = time.monotonic() + timeout
        while packet:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self._write], [], remaining)[1]:
                raise CDPError('브라우저 제어 명령 전송 시간이 초과되었습니다.')
            try:
                packet = packet[os.write(self._write, packet):]
            except BlockingIOError:
                continue
            except OSError:
                raise CDPError('브라우저 제어 pipe에 명령을 전달하지 못했습니다.') from None
        while True:
            response = self._receive(deadline)
            if response is None:
                raise CDPResponseTimeout('브라우저 제어 명령 응답 시간이 초과되었습니다.')
            if type(response.get('id')) is int and response['id'] == self._serial:
                if 'error' in response or not isinstance(response.get('result'), dict):
                    raise CDPError('브라우저 제어 명령이 실패했습니다.')
                return response['result']
            if 'method' in response:
                self._event(response)

    def next_event(self, timeout=1):
        if self._events:
            return self._events.popleft()
        deadline = time.monotonic() + timeout
        while True:
            value = self._receive(deadline)
            if value is None:
                return None
            if 'method' in value:
                return value

    def close(self):
        # waitpid identifies our unreaped child; never kill a saved, unrelated PID.
        if self.pid is not None and self.poll() is None:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 5
            while self.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if self.poll() is None:
                try:
                    os.kill(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(self.pid, 0)
                self.returncode = -signal.SIGKILL
        for name in ('_read', '_write'):
            fd = getattr(self, name, None)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)
