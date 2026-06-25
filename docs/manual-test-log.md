# Manual / hardware test log

Hardware acceptance criteria (`VALIDATION.md` V5.7, V6.8–V6.12) require a real
Chromecast Ultra + TV on the LAN. Run them with:

```bash
export VIDSTREAMER_TEST_DEVICE="Living Room"   # device name or IP
pytest tests/test_p6_e2e.py -k real -v          # un-skips the gated tests
```

These are currently `pytest.skip` placeholders that document the manual steps;
record each real run below.

| Date | Device | Criterion | Source | Result | Notes |
|------|--------|-----------|--------|--------|-------|
|      |        | V6.8 plays + controls |  |  |  |
|      |        | V6.9 embedded subs |  |  |  |
|      |        | V6.10 sidecar subs |  |  |  |
|      |        | V6.11 web URL |  |  |  |
|      |        | V6.12 seek A/V sync |  |  |  |
