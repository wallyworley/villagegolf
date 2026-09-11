"""GolfWorker mutation-exclusivity regressions with a fake, in-process golf service."""
import importlib
import os
import threading
import time
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet


class GolfWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = patch.dict(os.environ, {
            "SECRET_KEY": "test-only-session-key", "USER_DB_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "DISABLE_REQUEST_WATCHER": "1", "SESSION_COOKIE_SECURE": "0", "TRUST_PROXY": "0",
        })
        cls.environment.start()
        with patch("dotenv.load_dotenv"):
            cls.api = importlib.import_module("app")

    @classmethod
    def tearDownClass(cls):
        cls.environment.stop()

    @staticmethod
    def _make_fake_service_class():
        """Build a fresh fake GolfService class with its own entered/release Events."""
        entered = threading.Event()
        release = threading.Event()

        class _FakeGolfService:
            def book_tee_time(self, **kwargs):
                entered.set()
                release.wait(timeout=10)
                return {"success": True}

            def delete_request(self, **kwargs):
                return {"success": True}

            def get_available_times(self, **kwargs):
                return {"success": True, "times": []}

        _FakeGolfService.entered = entered
        _FakeGolfService.release = release
        return _FakeGolfService

    @staticmethod
    def _stop_worker(worker):
        """Stop the worker's job thread so it does not leak across tests."""
        worker._job_q.put(None)
        worker._thread.join(timeout=5)

    def _start_worker(self):
        import golf_service

        fake_cls = self._make_fake_service_class()
        patcher = patch.object(golf_service, "GolfService", fake_cls)
        patcher.start()
        self.addCleanup(patcher.stop)
        worker = self.api.GolfWorker()
        self.addCleanup(self._stop_worker, worker)
        return worker, fake_cls

    def test_second_mutation_for_same_account_is_refused_until_first_finishes(self):
        worker, fake = self._start_worker()
        first_result = {}

        def run_first():
            first_result["value"] = worker.call("book_tee_time", exclusive_key="u")

        thread = threading.Thread(target=run_first)
        thread.start()
        self.assertTrue(fake.entered.wait(timeout=5))

        started = time.monotonic()
        second = worker.call("book_tee_time", exclusive_key="u")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0)
        self.assertTrue(second.get("busy"))

        third = worker.call("delete_request", exclusive_key="u")
        self.assertTrue(third.get("busy"))

        fake.release.set()
        thread.join(timeout=5)
        self.assertTrue(first_result["value"].get("success"))

        fourth = worker.call("delete_request", exclusive_key="u")
        self.assertTrue(fourth.get("success"))

    def test_other_account_is_not_blocked(self):
        worker, fake = self._start_worker()

        thread = threading.Thread(target=lambda: worker.call("book_tee_time", exclusive_key="u"))
        thread.start()
        self.assertTrue(fake.entered.wait(timeout=5))

        def release_soon():
            time.sleep(0.2)
            fake.release.set()

        threading.Thread(target=release_soon).start()

        # The single worker thread is occupied with "u", so this call blocks
        # in the queue until "u" finishes; release_soon keeps that under 5s.
        result = worker.call("delete_request", exclusive_key="v")
        self.assertFalse(result.get("busy"))
        thread.join(timeout=5)

    def test_non_mutation_calls_ignore_exclusive_key(self):
        worker, fake = self._start_worker()

        thread = threading.Thread(target=lambda: worker.call("book_tee_time", exclusive_key="u"))
        thread.start()
        self.assertTrue(fake.entered.wait(timeout=5))
        fake.release.set()
        thread.join(timeout=5)

        result = worker.call("get_available_times", exclusive_key="u")
        self.assertFalse(result.get("busy"))
        self.assertTrue(result.get("success"))

    def test_timeout_keeps_key_until_worker_finishes(self):
        worker, fake = self._start_worker()
        worker._CALL_TIMEOUT_SECONDS = 0.2

        # The fake's book_tee_time blocks on fake.release, so with a 0.2s call
        # timeout, call() gives up on this thread long before the job (still
        # running on the worker thread) finishes.
        started = time.monotonic()
        first = worker.call("book_tee_time", exclusive_key="u")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0)
        self.assertFalse(first.get("success"))
        self.assertTrue(first.get("unknown"))

        # The exclusivity key must still be held: the job is still running,
        # only the caller gave up. A regression that released the key on
        # timeout would let this second call through to the worker (queued or
        # executing) instead of being refused as busy, and this assertion
        # would fail.
        second = worker.call("book_tee_time", exclusive_key="u")
        self.assertTrue(second.get("busy"))

        fake.release.set()
        deadline = time.monotonic() + 5
        released = False
        while time.monotonic() < deadline:
            with worker._inflight_lock:
                released = "u" not in worker._inflight
            if released:
                break
            time.sleep(0.05)
        self.assertTrue(released, "worker never released the exclusivity key")

        third = worker.call("delete_request", exclusive_key="u")
        self.assertTrue(third.get("success"))

    def test_base_exception_from_service_is_reported_not_raised(self):
        import golf_service

        class _RaisingService:
            def delete_request(self, **kwargs):
                raise KeyboardInterrupt()

        patcher = patch.object(golf_service, "GolfService", _RaisingService)
        patcher.start()
        self.addCleanup(patcher.stop)
        worker = self.api.GolfWorker()
        # A re-raised BaseException terminates the worker thread, so cleanup
        # must not wait on a sentinel a dead thread will never drain; join
        # with a short timeout and tolerate a thread that is already gone.
        self.addCleanup(lambda: worker._thread.join(timeout=2))

        result = worker.call("delete_request", exclusive_key="u")
        self.assertFalse(result.get("success"))
        self.assertTrue(result.get("unknown"))
        with worker._inflight_lock:
            self.assertNotIn("u", worker._inflight)


if __name__ == "__main__":
    unittest.main()
