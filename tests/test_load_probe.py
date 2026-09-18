import asyncio
import importlib.util
import json
import unittest

from mttr_guard.load_probe import run_probe, validate


@unittest.skipUnless(importlib.util.find_spec("httpx"), "Install requirements-enterprise.txt")
class LoadProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.status, self.delay, self.payload = 200, 0, b'{"ready":true}'
        self.requests, self.handlers = [], set()
        async def handle(reader, writer):
            self.handlers.add(asyncio.current_task())
            try:
                self.requests.append(await reader.readuntil(b"\r\n\r\n"))
                await asyncio.sleep(self.delay)
                writer.write(f"HTTP/1.1 {self.status} Test\r\nContent-Length: {len(self.payload)}\r\nConnection: close\r\nLocation: http://127.0.0.1:1/forbidden\r\n\r\n".encode() + self.payload)
                await writer.drain()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()
                self.handlers.discard(asyncio.current_task())
        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.plan = {"base_url": f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}",
            "domain": "SYNTHETIC", "service": "checkout", "rate_per_second": 2, "duration_seconds": 1,
            "concurrency": 2, "request_timeout_seconds": 1, "max_error_ratio": 0, "max_p95_seconds": 1}

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        if self.handlers:
            await asyncio.gather(*self.handlers, return_exceptions=True)

    async def test_success_checks_actual_response_without_leaking_token(self):
        token = "secret" * 8
        report = await run_probe(self.plan, token)
        self.assertTrue(report["numeric_gate_met"])
        self.assertEqual(report["successful"], report["planned"])
        self.assertNotIn(token, json.dumps(report))
        self.assertTrue(all(b"GET /api/operations" in r for r in self.requests))

    async def test_overload_skips_count_as_failures_instead_of_lowering_offered_rate(self):
        self.delay = .2
        self.plan.update(rate_per_second=20, concurrency=1)
        report = await run_probe(self.plan, "t" * 40)
        self.assertGreater(report["skipped_concurrency"], 0)
        self.assertEqual(report["successful"] + report["failed_or_skipped"], report["planned"])
        self.assertFalse(report["numeric_gate_met"])

    async def test_timeout_readiness_and_redirects_fail_closed(self):
        self.plan.update(rate_per_second=1, request_timeout_seconds=.1)
        for status, payload, delay in ((200, b'{"ready":true}', .2), (200, b'{"ready":false}', 0),
                                       (302, b'{}', 0), (429, b'{}', 0), (200, b'x' * 65537, 0)):
            with self.subTest(status=status, delay=delay):
                self.status, self.payload, self.delay = status, payload, delay
                report = await run_probe(self.plan, "t" * 40)
                self.assertEqual(report["failed_or_skipped"], 1)
                self.assertFalse(report["numeric_gate_met"])

    async def test_remote_plaintext_credentials_and_unbounded_load_are_rejected(self):
        for changed in ({"base_url": "http://monitoring.example"}, {"base_url": "https://user:secret@example.org"},
                        {"base_url": "https://example.org/api"}, {"concurrency": 10000}, {"rate_per_second": float("nan")}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                validate({**self.plan, **changed})
