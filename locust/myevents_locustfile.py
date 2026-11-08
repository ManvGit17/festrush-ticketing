from fest_client import sign_up_and_in

from locust import HttpUser, between, task


class MyEventsUser(HttpUser):
    wait_time = between(1, 2)

    def on_start(self) -> None:
        sign_up_and_in(self)

    @task
    def view_my_events(self) -> None:
        self.client.get("/my-events")
