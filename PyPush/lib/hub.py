import threading
import datetime
import Queue

from . import (
    iLib,
    ble,
    microbot,
    exceptions,
    async,
)


class PushHub(iLib.iHub):

    def __init__(self, bleConfig, keyDb, maxMicrobotAge=24 * 60 * 60):
        self._maxAge = maxMicrobotAge
        self._keyDb = keyDb
        self._mutex = threading.RLock()
        self._microbots = {}  # uid -> microbot object

        self._newMbCbs = async.SubscribeHub()
        self._lostMbCbs = async.SubscribeHub()

        self._ble = ble.getLib(bleConfig)
        self._ble.onScan(self._onBleScan)
        self._gcMicrobots()
        self._started = False

    def start(self):
        """Start daemon threads."""
        self._ble.start()
        self._started = True

    def onMicrobot(self, onDiscovered, onLost):
        handles = []
        if onDiscovered:
            assert callable(onDiscovered)
            handles.append(self._newMbCbs.subscribe(onDiscovered))
        if onLost:
            assert callable(onLost)
            handles.append(self._lostMbCbs.subscribe(onLost))

        if not handles:
            raise Exception("No callbacks provided")

        return async.MultiHandle(handles)

    def getMicrobot(self, nameOrUid, timeout=0):
        if not self._started:
            raise exceptions.PyPushException("Microbot hub is not started.")
        key = nameOrUid.lower()
        isMyBot = lambda mb: key in (mb.getUID().lower(), mb.getName().lower())

        if timeout <= 0:
            timeout = None

        with self._mutex:
            for bot in self._microbots.itervalues():
                if isMyBot(bot):
                    return bot

            # no bot found in the cache. Register the 'new microbot' callback
            # and wait for the timeout
            _q_ = Queue.Queue()

            def _onNewMb(mb):
                if isMyBot(mb):
                    _q_.put(mb)
            handle = self.onMicrobot(_onNewMb, None)

        try:
            return _q_.get(timeout=timeout)
        except Queue.Empty:
            raise exceptions.Timeout("Failed to get microbot in time.")
        finally:
            handle.cancel()

    def createMicrobotFromUUID(self, uuid):
        with self._mutex:
            try:
                rv = self._microbots[uuid]
            except KeyError:
                # Microbot does not exist, create the object
                _bleMb = self._ble.createMicrobotFromUUID(uuid)
                rv = microbot.MicrobotPush(self._ble, _bleMb, self._keyDb)
                self._microbots[_bleMb.getUID()] = rv 
        return rv

    def getAllMicrobots(self):
        return self._microbots.values()

    def _onBleScan(self, bleMicrobot):
        uid = bleMicrobot.getUID()
        isNew = False
        mb = None

        with self._mutex:
            isNew = uid not in self._microbots
            if isNew:
                mb = microbot.MicrobotPush(self._ble, bleMicrobot, self._keyDb)
                self._microbots[uid] = mb

        if isNew:
            assert mb
            # Execute callbacks for the 'new microbot' event
            self._newMbCbs.fireSubscribers(mb)

    _gcTimer = None

    def _gcMicrobots(self):
        """This method removes all long-lost microbots.

        It auto-schedules itself for the periodic execution.
        """
        cutoff = datetime.datetime.now() - datetime.timedelta(seconds=self._maxAge)
        toDelete = []

        with self._mutex:
            for (key, mb) in self._microbots.iteritems():
                if mb.getLastSeen() < cutoff:
                    toDelete.append(key)
            for key in toDelete:
                self._microbots.pop(key)

        # Schedule next execution
        self._gcTimer = threading.Timer(self._maxAge / 4, self._gcMicrobots)
        self._gcTimer.daemon = True
        self._gcTimer.start()
