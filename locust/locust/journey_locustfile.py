"""The full student journey: browse, hold a seat, review, pay.

Lives at its original path (locust/locust/) because the course may refer to it there.
"""

import random
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fest_client import EVENT_FORM, form_token, sign_up_and_in

from locust import HttpUser, between, task


class FestJourneyUser(HttpUser):
    wait_time = between(1, 2)

    def on_start(self) -> None:
        sign_up_and_in(self)
        self.held: set[str] = set()

    @task
    def journey(self) -> None:
        page = self.client.get("/events")
        open_events = [e for e in EVENT_FORM.findall(page.text) if e not in self.held]
        if open_events:
            event_id = random.choice(open_events)
            self.client.post(
                f"/register_event/{event_id}",
                data={"csrf_token": form_token(self, "/login")},
                name="POST /register_event/[id]",
            )
            self.held.add(event_id)
        self.client.get("/my-events")
        self.client.get("/checkout")
        # Paying twice with the same key pays once; a fresh key per attempt is what the form does.
        self.client.post(
            "/checkout",
            data={"csrf_token": form_token(self, "/login"), "idempotency_key": uuid.uuid4().hex},
            name="POST /checkout",
        )
