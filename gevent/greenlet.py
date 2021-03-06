# Copyright (c) 2009-2012 Denis Bilenko. See LICENSE for details.

import sys
from gevent.hub import GreenletExit
from gevent.hub import PY3
from gevent.hub import PYPY
from gevent.hub import Waiter
from gevent.hub import get_hub
from gevent.hub import getcurrent
from gevent.hub import greenlet
from gevent.hub import iwait
from gevent.hub import reraise
from gevent.hub import wait
from gevent.timeout import Timeout
from gevent._tblib import dump_traceback
from gevent._tblib import load_traceback
from collections import deque


__all__ = ['Greenlet',
           'joinall',
           'killall']


if PYPY:
    import _continuation
    _continulet = _continuation.continulet


class SpawnedLink(object):
    """A wrapper around link that calls it in another greenlet.

    Can be called only from main loop.
    """
    __slots__ = ['callback']

    def __init__(self, callback):
        if not callable(callback):
            raise TypeError("Expected callable: %r" % (callback, ))
        self.callback = callback

    def __call__(self, source):
        g = greenlet(self.callback, get_hub())
        g.switch(source)

    def __hash__(self):
        return hash(self.callback)

    def __eq__(self, other):
        return self.callback == getattr(other, 'callback', other)

    def __str__(self):
        return str(self.callback)

    def __repr__(self):
        return repr(self.callback)

    def __getattr__(self, item):
        assert item != 'callback'
        return getattr(self.callback, item)


class SuccessSpawnedLink(SpawnedLink):
    """A wrapper around link that calls it in another greenlet only if source succeed.

    Can be called only from main loop.
    """
    __slots__ = []

    def __call__(self, source):
        if source.successful():
            return SpawnedLink.__call__(self, source)


class FailureSpawnedLink(SpawnedLink):
    """A wrapper around link that calls it in another greenlet only if source failed.

    Can be called only from main loop.
    """
    __slots__ = []

    def __call__(self, source):
        if not source.successful():
            return SpawnedLink.__call__(self, source)


class _lazy(object):

    def __init__(self, func):
        self.data = (func, func.__name__)

    def __get__(self, inst, class_):
        if inst is None:
            return self

        func, name = self.data
        value = func(inst)
        inst.__dict__[name] = value
        return value


class Greenlet(greenlet):
    """A light-weight cooperatively-scheduled execution unit."""

    value = None
    _exc_info = ()
    _notifier = None
    _start_event = None
    args = ()

    def __init__(self, run=None, *args, **kwargs):
        hub = get_hub()
        greenlet.__init__(self, parent=hub)
        if run is not None:
            self._run = run
        if args:
            self.args = args
        self.kwargs = kwargs

    @_lazy
    def _links(self):
        return deque()

    def _has_links(self):
        return '_links' in self.__dict__ and self._links

    def _raise_exception(self):
        reraise(*self.exc_info)

    @property
    def loop(self):
        # needed by killall
        return self.parent.loop

    def __bool__(self):
        return self._start_event is not None and self._exc_info is Greenlet._exc_info
    __nonzero__ = __bool__

    if PYPY:
        # oops - pypy's .dead relies on __nonzero__ which we overriden above
        @property
        def dead(self):
            return self._greenlet__started and not (self._greenlet__main or _continulet.is_pending(self))

    @property
    def started(self):
        # DEPRECATED
        return bool(self)

    def ready(self):
        """Return true if and only if the greenlet has finished execution."""
        return self.dead or self._exc_info

    def successful(self):
        """Return true if and only if the greenlet has finished execution successfully,
        that is, without raising an error."""
        return self._exc_info and self._exc_info[1] is None

    def __repr__(self):
        classname = self.__class__.__name__
        result = '<%s at %s' % (classname, hex(id(self)))
        formatted = self._formatinfo()
        if formatted:
            result += ': ' + formatted
        return result + '>'

    def _formatinfo(self):
        try:
            return self._formatted_info
        except AttributeError:
            pass
        try:
            result = getfuncname(self.__dict__['_run'])
        except Exception:
            pass
        else:
            args = []
            if self.args:
                args = [repr(x)[:50] for x in self.args]
            if self.kwargs:
                args.extend(['%s=%s' % (key, repr(value)[:50]) for (key, value) in self.kwargs.items()])
            if args:
                result += '(' + ', '.join(args) + ')'
            # it is important to save the result here, because once the greenlet exits '_run' attribute will be removed
            self._formatted_info = result
            return result
        return ''

    @property
    def exception(self):
        """Holds the exception instance raised by the function if the greenlet has finished with an error.
        Otherwise ``None``.
        """
        return self._exc_info[1] if self._exc_info else None

    @property
    def exc_info(self):
        """Holds the exc_info three-tuple raised by the function if the greenlet finished with an error.
        Otherwise a false value."""
        e = self._exc_info
        if e:
            return (e[0], e[1], load_traceback(e[2]))

    def throw(self, *args):
        """Immediatelly switch into the greenlet and raise an exception in it.

        Should only be called from the HUB, otherwise the current greenlet is left unscheduled forever.
        To raise an exception in a safe manner from any greenlet, use :meth:`kill`.

        If a greenlet was started but never switched to yet, then also
        a) cancel the event that will start it
        b) fire the notifications as if an exception was raised in a greenlet
        """
        if self._start_event is None:
            self._start_event = _dummy_event
        else:
            self._start_event.stop()
        try:
            greenlet.throw(self, *args)
        finally:
            if self._exc_info is Greenlet._exc_info and self.dead:
                # the greenlet was never switched to before and it will never be, _report_error was not called
                # the result was not set and the links weren't notified. let's do it here.
                # checking that self.dead is true is essential, because throw() does not necessarily kill the greenlet
                # (if the exception raised by throw() is caught somewhere inside the greenlet).
                if len(args) == 1:
                    arg = args[0]
                    #if isinstance(arg, type):
                    if type(arg) is type(Exception):
                        args = (arg, arg(), None)
                    else:
                        args = (type(arg), arg, None)
                elif not args:
                    args = (GreenletExit, GreenletExit(), None)
                self._report_error(args)

    def start(self):
        """Schedule the greenlet to run in this loop iteration"""
        if self._start_event is None:
            self._start_event = self.parent.loop.run_callback(self.switch)

    def start_later(self, seconds):
        """Schedule the greenlet to run in the future loop iteration *seconds* later"""
        if self._start_event is None:
            self._start_event = self.parent.loop.timer(seconds)
            self._start_event.start(self.switch)

    @classmethod
    def spawn(cls, *args, **kwargs):
        """Return a new :class:`Greenlet` object, scheduled to start.

        The arguments are passed to :meth:`Greenlet.__init__`.
        """
        g = cls(*args, **kwargs)
        g.start()
        return g

    @classmethod
    def spawn_later(cls, seconds, *args, **kwargs):
        """Return a Greenlet object, scheduled to start *seconds* later.

        The arguments are passed to :meth:`Greenlet.__init__`.
        """
        g = cls(*args, **kwargs)
        g.start_later(seconds)
        return g

    def kill(self, exception=GreenletExit, block=True, timeout=None):
        """Raise the ``exception`` in the greenlet.

        If ``block`` is ``True`` (the default), wait until the greenlet dies or the optional timeout expires.
        If block is ``False``, the current greenlet is not unscheduled.

        The function always returns ``None`` and never raises an error.

        .. note::

            Depending on what this greenlet is executing and the state of the event loop,
            the exception may or may not be raised immediately when this greenlet resumes
            execution. It may be raised an a subsequent green call, or, if this greenlet
            exits before making such a call, it may not be raised at all. As of 1.1, an example
            where the exception is raised later is if this greenlet had called ``sleep(0)``; an
            example where the exception is raised immediately is if this greenlet had called ``sleep(0.1)``.

        See also :func:`gevent.kill`.

        .. versionchanged:: 0.13.0
            *block* is now ``True`` by default.
        """
        # XXX this function should not switch out if greenlet is not started but it does
        # XXX fix it (will have to override 'dead' property of greenlet.greenlet)
        if self._start_event is None:
            self._start_event = _dummy_event
        else:
            self._start_event.stop()
        if not self.dead:
            waiter = Waiter()
            self.parent.loop.run_callback(_kill, self, exception, waiter)
            if block:
                waiter.get()
                self.join(timeout)
        # it should be OK to use kill() in finally or kill a greenlet from more than one place;
        # thus it should not raise when the greenlet is already killed (= not started)

    def get(self, block=True, timeout=None):
        """Return the result the greenlet has returned or re-raise the exception it has raised.

        If block is ``False``, raise :class:`gevent.Timeout` if the greenlet is still alive.
        If block is ``True``, unschedule the current greenlet until the result is available
        or the timeout expires. In the latter case, :class:`gevent.Timeout` is raised.
        """
        if self.ready():
            if self.successful():
                return self.value
            self._raise_exception()
        if not block:
            raise Timeout()

        switch = getcurrent().switch
        self.rawlink(switch)
        try:
            t = Timeout.start_new(timeout)
            try:
                result = self.parent.switch()
                assert result is self, 'Invalid switch into Greenlet.get(): %r' % (result, )
            finally:
                t.cancel()
        except:
            # unlinking in 'except' instead of finally is an optimization:
            # if switch occurred normally then link was already removed in _notify_links
            # and there's no need to touch the links set.
            # Note, however, that if "Invalid switch" assert was removed and invalid switch
            # did happen, the link would remain, causing another invalid switch later in this greenlet.
            self.unlink(switch)
            raise

        if self.ready():
            if self.successful():
                return self.value
            self._raise_exception()

    def join(self, timeout=None):
        """Wait until the greenlet finishes or *timeout* expires.
        Return ``None`` regardless.
        """
        if self.ready():
            return

        switch = getcurrent().switch
        self.rawlink(switch)
        try:
            t = Timeout.start_new(timeout)
            try:
                result = self.parent.switch()
                assert result is self, 'Invalid switch into Greenlet.join(): %r' % (result, )
            finally:
                t.cancel()
        except Timeout as ex:
            self.unlink(switch)
            if ex is not t:
                raise
        except:
            self.unlink(switch)
            raise

    def _report_result(self, result):
        self._exc_info = (None, None, None)
        self.value = result
        if self._has_links() and not self._notifier:
            self._notifier = self.parent.loop.run_callback(self._notify_links)

    def _report_error(self, exc_info):
        if isinstance(exc_info[1], GreenletExit):
            self._report_result(exc_info[1])
            return

        self._exc_info = exc_info[0], exc_info[1], dump_traceback(exc_info[2])

        if self._has_links() and not self._notifier:
            self._notifier = self.parent.loop.run_callback(self._notify_links)

        try:
            self.parent.handle_error(self, *exc_info)
        finally:
            del exc_info

    def run(self):
        try:
            if self._start_event is None:
                self._start_event = _dummy_event
            else:
                self._start_event.stop()
            try:
                result = self._run(*self.args, **self.kwargs)
            except:
                self._report_error(sys.exc_info())
                return
            self._report_result(result)
        finally:
            self.__dict__.pop('_run', None)
            self.__dict__.pop('args', None)
            self.__dict__.pop('kwargs', None)

    def rawlink(self, callback):
        """Register a callable to be executed when the greenlet finishes the execution.

        WARNING: the callable will be called in the HUB greenlet.
        """
        if not callable(callback):
            raise TypeError('Expected callable: %r' % (callback, ))
        self._links.append(callback)
        if self.ready() and self._links and not self._notifier:
            self._notifier = self.parent.loop.run_callback(self._notify_links)

    def link(self, callback, SpawnedLink=SpawnedLink):
        """Link greenlet's completion to a callable.

        The *callback* will be called with this instance as an argument
        once this greenlet's dead. A callable is called in its own greenlet.
        """
        self.rawlink(SpawnedLink(callback))

    def unlink(self, callback):
        """Remove the callback set by :meth:`link` or :meth:`rawlink`"""
        try:
            self._links.remove(callback)
        except ValueError:
            pass

    def link_value(self, callback, SpawnedLink=SuccessSpawnedLink):
        """Like :meth:`link` but *callback* is only notified when the greenlet has completed successfully"""
        self.link(callback, SpawnedLink=SpawnedLink)

    def link_exception(self, callback, SpawnedLink=FailureSpawnedLink):
        """Like :meth:`link` but *callback* is only notified when the greenlet dies because of unhandled exception"""
        self.link(callback, SpawnedLink=SpawnedLink)

    def _notify_links(self):
        while self._links:
            link = self._links.popleft()
            try:
                link(self)
            except:
                self.parent.handle_error((link, self), *sys.exc_info())


class _dummy_event(object):

    def stop(self):
        pass


_dummy_event = _dummy_event()


def _kill(greenlet, exception, waiter):
    try:
        greenlet.throw(exception)
    except:
        # XXX do we need this here?
        greenlet.parent.handle_error(greenlet, *sys.exc_info())
    waiter.switch()


def joinall(greenlets, timeout=None, raise_error=False, count=None):
    """
    Wait for the ``greenlets`` to finish.

    :param greenlets: A sequence (supporting :func:`len`) of greenlets to wait for.
    :keyword float timeout: If given, the maximum number of seconds to wait.
    :return: A sequence of the greenlets that finished before the timeout (if any)
        expired.
    """
    if not raise_error:
        return wait(greenlets, timeout=timeout, count=count)

    done = []
    for obj in iwait(greenlets, timeout=timeout, count=count):
        if getattr(obj, 'exception', None) is not None:
            if hasattr(obj, '_raise_exception'):
                obj._raise_exception()
            else:
                raise obj.exception
        done.append(obj)
    return done


def _killall3(greenlets, exception, waiter):
    diehards = []
    for g in greenlets:
        if not g.dead:
            try:
                g.throw(exception)
            except:
                g.parent.handle_error(g, *sys.exc_info())
            if not g.dead:
                diehards.append(g)
    waiter.switch(diehards)


def _killall(greenlets, exception):
    for g in greenlets:
        if not g.dead:
            try:
                g.throw(exception)
            except:
                g.parent.handle_error(g, *sys.exc_info())


def killall(greenlets, exception=GreenletExit, block=True, timeout=None):
    """
    Forceably terminate all the ``greenlets`` by causing them to raise ``exception``.

    :param greenlets: A bounded iterable of the non-None greenlets to terminate.
       *All* the items in this iterable must be greenlets that belong to the same thread.
    :keyword exception: The exception to raise in the greenlets. By default this is
        :class:`GreenletExit`.
    :keyword bool block: If True (the default) then this function only returns when all the
        greenlets are dead; the current greenlet is unscheduled during that process.
        If greenlets ignore the initial exception raised in them,
        then they will be joined (with :func:`gevent.joinall`) and allowed to die naturally.
        If False, this function returns immediately and greenlets will raise
        the exception asynchronously.
    :keyword float timeout: A time in seconds to wait for greenlets to die. If given, it is
        only honored when ``block`` is True.
    :raises Timeout: If blocking and a timeout is given that elapses before
        all the greenlets are dead.
    """
    # support non-indexable containers like iterators or set objects
    greenlets = list(greenlets)
    if not greenlets:
        return
    loop = greenlets[0].loop
    if block:
        waiter = Waiter()
        loop.run_callback(_killall3, greenlets, exception, waiter)
        t = Timeout.start_new(timeout)
        try:
            alive = waiter.get()
            if alive:
                joinall(alive, raise_error=False)
        finally:
            t.cancel()
    else:
        loop.run_callback(_killall, greenlets, exception)


if PY3:
    _meth_self = "__self__"
else:
    _meth_self = "im_self"


def getfuncname(func):
    if not hasattr(func, _meth_self):
        try:
            funcname = func.__name__
        except AttributeError:
            pass
        else:
            if funcname != '<lambda>':
                return funcname
    return repr(func)
