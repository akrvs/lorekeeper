# checkout-v2 Architecture

`checkout-v2` is the one-click checkout service that replaced the legacy cart.

It calls the [[payments-service]] to authorize charges. All third-party
credentials it needs are governed by the [[api-keys-policy]] — notably the
`PAYMENTS_API_KEY`, whose absence in production caused the recent outage.
