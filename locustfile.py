"""
locustfile.py
-------------
Load test for the B2B AI Lead Qualifier V2 API.

Simulates the real async pattern:
  1. POST /token         → obtain JWT
  2. POST /lead          → 202 Accepted + thread_id
  3. GET  /status/{id}   → poll until completed | pending_review | error

Metrics captured:
  - API latency: POST /lead response time (immediate, measures API + ARQ enqueue)
  - Worker resolution time: measured via a custom event emitted by the Poller task
    (`lead_resolution_time`), which is the wall-clock time from POST /lead until
    the status transitions to a terminal state.

Usage:
  locust -f locustfile.py --host http://localhost:8080 \
         --users 20 --spawn-rate 2 --run-time 2m --headless

Anti-pattern avoided:
  - Polling uses time.sleep() in a separate greenlet so Locust's I/O metrics
    are never blocked by the worker resolution delay.
"""

from __future__ import annotations

import time
import uuid
from typing import Optional

import gevent
from locust import HttpUser, between, events, task
from locust.exception import StopUser

# ── Constants ─────────────────────────────────────────────────────────────────

POLL_INTERVAL: float = 2.0          # seconds between /status polls
POLL_TIMEOUT: float = 120.0         # max seconds to wait for resolution
TERMINAL_STATUSES: set[str] = {"completed", "pending_review", "error"}

# ── Sample leads (rotated per task invocation) ────────────────────────────────

SAMPLE_LEADS: list[str] = [
    (
        "Siamo una startup SaaS B2B con 25 dipendenti. "
        "Cerchiamo supporto per sviluppo software backend (Python/FastAPI), "
        "architettura cloud AWS e consulenza AI. Budget annuo ~120k€."
    ),
    (
        "Agenzia di marketing digitale — 8 persone. Necessitiamo servizi di "
        "SEO tecnico, content marketing e social media management. "
        "Disponibilità mensile 3.000€."
    ),
    (
        "PMI manifatturiera, 60 addetti. Richiesta per implementazione ERP "
        "custom integrato con CRM Salesforce e automazione magazzino. "
        "Budget progetto 200k€."
    ),
    (
        "Studio legale, 12 avvocati. Interesse per software gestione pratiche, "
        "firma digitale e fatturazione elettronica. Budget 15k€ una tantum."
    ),
]


# ── Poller greenlet ───────────────────────────────────────────────────────────

def _poll_status(
    client,
    thread_id: str,
    headers: dict[str, str],
    submit_time: float,
) -> None:
    """
    Run in a separate gevent greenlet so it never blocks Locust I/O tracking.

    Polls GET /status/{thread_id} every POLL_INTERVAL seconds.
    Emits a custom `lead_resolution_time` event on terminal state.
    """
    deadline = time.monotonic() + POLL_TIMEOUT

    while time.monotonic() < deadline:
        gevent.sleep(POLL_INTERVAL)

        with client.get(
            f"/status/{thread_id}",
            headers=headers,
            name="/status/[thread_id]",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                data: dict = resp.json()
                status: str = data.get("status", "")

                if status in TERMINAL_STATUSES:
                    resolution_ms = (time.monotonic() - submit_time) * 1000
                    events.request.fire(
                        request_type="POLL",
                        name="lead_resolution_time",
                        response_time=resolution_ms,
                        response_length=len(resp.content),
                        exception=None if status != "error" else Exception(
                            data.get("error_detail", "unknown error")
                        ),
                        context={},
                    )
                    resp.success()
                    return
                else:
                    resp.success()  # intermediate status — keep polling

            elif resp.status_code == 404:
                resp.failure(f"thread_id {thread_id!r} not found")
                return
            else:
                resp.failure(f"unexpected status code {resp.status_code}")

    # Timeout: emit failure event
    resolution_ms = (time.monotonic() - submit_time) * 1000
    events.request.fire(
        request_type="POLL",
        name="lead_resolution_time",
        response_time=resolution_ms,
        response_length=0,
        exception=TimeoutError(f"lead {thread_id!r} did not resolve in {POLL_TIMEOUT}s"),
        context={},
    )


# ── Locust User ───────────────────────────────────────────────────────────────

class B2BLeadUser(HttpUser):
    """
    Simulates a B2B client tenant.

    on_start: authenticates once and stores the JWT.
    qualify_lead: the main task — POST /lead + async polling.
    """

    wait_time = between(3, 8)   # realistic think time between tasks

    # Tenant credentials (Locust reads --host; username doubles as tenant_id)
    _username: str = "tenant-load-test"
    _password: str = ""

    def on_start(self) -> None:
        """Authenticate once per simulated user and store the bearer token."""
        self._jwt: Optional[str] = None
        self._headers: dict[str, str] = {}

        with self.client.post(
            "/token",
            json={"username": self._username, "password": self._password},
            name="/token",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                self._jwt = resp.json().get("access_token")
                self._headers = {"Authorization": f"Bearer {self._jwt}"}
                resp.success()
            else:
                resp.failure(f"Auth failed: {resp.status_code} {resp.text}")
                raise StopUser()

    @task
    def qualify_lead(self) -> None:
        """
        Core task: submit a lead (POST /lead) and spawn a polling greenlet.

        The main greenlet returns immediately after the 202 so Locust measures
        only the API round-trip. The polling greenlet measures worker resolution
        time independently via a custom event.
        """
        if not self._jwt:
            raise StopUser()

        # Rotate through sample leads by index derived from thread_id
        lead_index = int(uuid.uuid4().int % len(SAMPLE_LEADS))
        payload = {
            "raw_text": SAMPLE_LEADS[lead_index],
            "lead_id": str(uuid.uuid4()),
        }

        submit_time = time.monotonic()

        with self.client.post(
            "/lead",
            json=payload,
            headers=self._headers,
            name="POST /lead",
            catch_response=True,
        ) as resp:
            if resp.status_code == 202:
                thread_id: str = resp.json().get("thread_id", "")
                if not thread_id:
                    resp.failure("202 but missing thread_id in response")
                    return
                resp.success()

                # Spawn polling in background — does NOT block this task
                gevent.spawn(
                    _poll_status,
                    self.client,
                    thread_id,
                    dict(self._headers),  # copy so mutation-safe
                    submit_time,
                )

            elif resp.status_code == 401:
                resp.failure("Unauthorized — token expired or invalid")
                raise StopUser()
            else:
                resp.failure(f"Expected 202, got {resp.status_code}: {resp.text[:200]}")
