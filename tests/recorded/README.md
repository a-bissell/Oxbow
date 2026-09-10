Recorded live API responses for replay tests.

Produce them on a machine with network access and MP_API_KEY:

    oxide-triage warm-cache --record tests/recorded

One JSON file per request (host/key.json: url, params, response). Headers are never recorded.
While this directory holds only this README, tests/test_recorded.py is skipped.
