# API reference

Generated from the OpenAPI schema by `scripts/build_docs.py`; do not edit by hand.
Authenticate with `Authorization: Bearer <token>` from `POST /api/login`. Writes accept an
`Idempotency-Key` header: send the same key to retry safely.

## `GET /api/events`

List Events

| Status | Meaning |
| :-- | :-- |
| 200 | Successful Response |

## `GET /api/events/{event_id}`

Get Event

| Status | Meaning |
| :-- | :-- |
| 200 | Successful Response |
| 404 | Not Found |
| 422 | Validation Error |

## `POST /api/events/{event_id}/reserve`

Hold one seat for ten minutes (configurable). Pay for it with ``POST /api/orders``.

| Status | Meaning |
| :-- | :-- |
| 200 | A retry with the same Idempotency-Key: the first answer, replayed |
| 201 | Successful Response |
| 403 | not_admitted: join the waiting room first |
| 404 | Not Found |
| 409 | sold_out, already_reserved or busy |
| 422 | Idempotency-Key reused for a different request |

## `POST /api/login`

Login

| Status | Meaning |
| :-- | :-- |
| 200 | Successful Response |
| 401 | Unauthorized |
| 422 | Validation Error |

## `GET /api/me/reservations`

My Reservations

| Status | Meaning |
| :-- | :-- |
| 200 | Successful Response |
| 422 | Validation Error |

## `POST /api/orders`

Make an order from every seat you currently hold. Then pay with ``POST /api/orders/{id}/pay``.

| Status | Meaning |
| :-- | :-- |
| 201 | Successful Response |
| 409 | no_holds |
| 422 | Unprocessable Content |

## `GET /api/orders/{order_id}`

Read Order

| Status | Meaning |
| :-- | :-- |
| 200 | Successful Response |
| 404 | Not Found |
| 422 | Validation Error |

## `POST /api/orders/{order_id}/pay`

Pay for an order. Safe to repeat: only one call ever reaches the payment provider.

| Status | Meaning |
| :-- | :-- |
| 200 | Successful Response |
| 404 | Not Found |
| 422 | Validation Error |

## `POST /api/queue/join`

Join the waiting room. Joining again keeps your place.

| Status | Meaning |
| :-- | :-- |
| 200 | Successful Response |
| 422 | Validation Error |

## `GET /api/queue/status`

Queue Status

| Status | Meaning |
| :-- | :-- |
| 200 | Successful Response |
| 422 | Validation Error |

