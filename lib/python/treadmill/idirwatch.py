"""Watches a directory for changes using inotify and generates events.

Usage:

    def on_created(path):
        pass

    def on_deleted(path):
        pass

    def main():
        watch = DirWatcher('/tmp')
        watch.on_created = on_created
        watch.on_deleted = on_deleted

        if watch.wait_for_events(timeout):
            watch.process_events()
"""
from __future__ import absolute_import

import sys  # pylint: disable=C0411
import errno  # pylint: disable=C0411

import collections
import logging
import os
import select
import enum

if os.name == 'nt':
    from .syscall import readdirchange
else:
    from .syscall import inotify

_LOGGER = logging.getLogger(__name__)


class DirWatcherEvent(enum.Enum):
    """DirWatcher's emitted event types.
    """
    # W0232: Class has no __init__ method
    # pylint: disable=W0232
    CREATED = 'created'
    DELETED = 'deleted'
    MODIFIED = 'modified'
    #: Fake event returned when more events where received than allowed to
    #: process in ``process_events``
    MORE_PENDING = 'more events pending'


class DirWatcher(object):
    """Directory watcher, invoking callbacks on file create/delete events."""

    __slots__ = (
        'event_list',
        'inotify',
        'on_created',
        'on_deleted',
        'on_modified',
        'poll',
        'watch_dir',
    )

    def __init__(self, watch_dir):
        self.watch_dir = watch_dir
        if os.name == 'nt':
            self.inotify = readdirchange.ReadDirChange()
            self.inotify.add_watch(watch_dir)
        else:
            self.inotify = inotify.Inotify(inotify.IN_CLOEXEC)
            self.inotify.add_watch(
                watch_dir,
                event_mask=(
                    inotify.IN_ATTRIB |
                    inotify.IN_CREATE |
                    inotify.IN_DELETE |
                    inotify.IN_DELETE_SELF |
                    inotify.IN_MODIFY |
                    inotify.IN_MOVE
                )
            )
        self.event_list = collections.deque()
        self.poll = select.poll()
        self.poll.register(self.inotify, select.POLLIN)
        self.on_created = self._noop
        self.on_deleted = self._noop
        self.on_modified = self._noop

    def _noop(self, event_src):
        """Default NOOP callback"""
        _LOGGER.debug('event on %r (watching %r)', event_src, self.watch_dir)

    def wait_for_events(self, timeout=-1):
        """Wait for directory change event for up to ``timeout`` seconds.

        :param timeout:
            Time in seconds to wait for an event (-1 means forever)
        :returns:
            ``True`` if events were received, ``False`` otherwise.
        """
        # We already have cached events
        if self.event_list:
            return True

        if timeout != -1:
            timeout *= 1000  # poll timeout is in milliseconds

        try:
            rc = self.poll.poll(timeout)
            return bool(rc)
        except select.error as err:
            if err[0] == errno.EINTR:
                return False
            raise

    def process_events(self, max_events=0, resume=False):
        """Process events received.

        This function will parse all received events and invoke the registered
        callbacks accordingly.

        :param ``int`` max_events:
            Maximum number of events to process
        :param ``bool`` resume:
            Continue processing any event that was still in the queue (do not
            read new ones).
        :returns ``list``:
            List of ``(DirWatcherEvent, <path>, <callback_return>)``.
        """
        if max_events <= 0:
            max_events = sys.maxint

        # If we are out of cached events, get more from inotify
        if not self.event_list and not resume:
            try:
                self.event_list.extend(self.inotify.read_events())
            except OSError as err:
                if err.errno == errno.EINTR:
                    return []
                else:
                    raise

        results = []
        step = 0
        while self.event_list:
            if step >= max_events:
                # We reach the max number of events we could process
                results.append(
                    (
                        DirWatcherEvent.MORE_PENDING,
                        None,
                        None,
                    )
                )
                break
            step += 1
            event = self.event_list.popleft()

            if (event.is_modify or
                    event.is_attrib):
                res = self.on_modified(event.src_path)
                results.append(
                    (
                        DirWatcherEvent.MODIFIED,
                        event.src_path,
                        res,
                    )
                )

            elif (event.is_delete or
                  event.is_moved_from or
                  event.is_delete_self):
                res = self.on_deleted(event.src_path)
                results.append(
                    (
                        DirWatcherEvent.DELETED,
                        event.src_path,
                        res,
                    )
                )

            elif (event.is_create or
                  event.is_moved_to):
                res = self.on_created(event.src_path)
                results.append(
                    (
                        DirWatcherEvent.CREATED,
                        event.src_path,
                        res,
                    )
                )

        return results
