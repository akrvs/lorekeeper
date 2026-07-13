# Incident Report — 2026-05-20 Checkout Outage

On 2026-05-20 the production checkout flow was down for ~25 minutes. Customers
could not complete payment.

Root cause: the rollout described in [[checkout-v2-architecture]] shipped without
the required secret, so the service crash-looped on boot.

Responders followed the [[oncall-runbook]] to roll forward and restore service.
