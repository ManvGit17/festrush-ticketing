"""The full student journey: browse, register for one event, review, check out.

Lives at its original path (locust/locust/) because the course may refer to it there.
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fest_client import EVENT_FORM, form_token, sign_up_and_in

from locust import HttpUser, between, task


class FestJourneyUser(HttpUser):
    wait_time = between(1, 2)

    def on_start(self) -> None:
        sign_up_and_in(self)
        self.registered: set[str] = set()

    @task
    def journey(self) -> None:
        page = self.client.get("/events")
        open_events = [e for e in EVENT_FORM.findall(page.text) if e not in self.registered]
        if open_events:
            event_id = random.choice(open_events)
            self.client.post(
                f"/register_event/{event_id}",
                data={"csrf_token": form_token(self, "/login")},
                name="POST /register_event/[id]",
            )
            self.registered.add(event_id)
        self.client.get("/my-events")
        self.client.get("/checkout")
