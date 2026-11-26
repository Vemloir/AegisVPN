# CDN monitoring: measurement versus client availability

Enabled CDN resources remain in automatic client fallback regardless of monitor
state. There are no standalone WL entries. A control-host probe cannot determine
reachability from every subscriber network and must never remove an enabled path
from subscription rendering.

The scheduler probes every ten minutes, with one retry after three seconds.
The probe now has three outcomes:

- `True`: origin marker received (existing XHTTP origin validation).
- `False`: explicit bad response, missing origin marker, or TLS verification
  failure. Three consecutive measured-failure cycles raise one grouped diagnostic
  incident. A subsequent valid origin response closes it immediately.
- `None`: incomplete measurement (timeout, DNS/connect failure, transport error).
  Reset streaks, retain the previously confirmed state, record diagnostic detail;
  **do not open/close an incident**. A previously confirmed real bad-response
  incident is not cleared merely because a later measurement times out.

Legacy incidents whose last detail was `TimeoutError`, `ClientConnectorError`,
or `ClientConnectorDNSError` are retired without sending a recovery notification.
Recovery text states that this is the observation time, not measured downtime.
The scheduler interval is not a measurement of outage duration.

Transport diagnostics contain exception type, phase (`dns`, `connect`,
`connect_tls`, `response_headers`) and elapsed seconds. TCP/TLS establishment is
not separately measured. Secret origin paths, response headers and exception
messages are not logged. Cancellation/programming failures are not converted
into successful measurements. Pin learning and client-side fallback are unchanged.

On 2026-11-17, 20 native HTTP probes before the change succeeded, as did both
endpoint probes after deployment. Historical transport timeouts were **not
reproduced or explained**. This change fixes misleading incident classification;
it does not claim to repair the underlying intermittent network condition.

Only bot/siteapi were recreated on control. Backup:
`/root/aegis/backups/cdn-gemini-ux-20261117T174437Z`.
