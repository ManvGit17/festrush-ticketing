from fest_client import sign_up_and_in

from locust import HttpUser, between, task


class CheckoutUser(HttpUser):
    wait_time = between(1, 2)

    def on_start(self) -> None:
        sign_up_and_in(self)

    @task
    def checkout(self) -> None:
        self.client.get("/checkout")
