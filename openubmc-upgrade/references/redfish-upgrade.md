# Redfish Upgrade Flow

## Discovery

Read the target's own:

~~~text
GET /redfish/v1/UpdateService
~~~

Use only its advertised upload URI. Prefer:

1. MultipartHttpPushUri
2. HttpPushUri
3. Actions/#UpdateService.SimpleUpdate

Require an in-process HTTPS client with Basic or session authentication sourced
from direct internal-development input, the task context, environment selectors,
or the selected credentials file. Reject a URI that is not relative to, or on
the same HTTPS origin as, the selected BMC.

The Target Runtime lane defaults to disabled certificate verification for internal BMC targets.
Set `allow_insecure_tls=false` when the target certificate is trusted. Standalone preflight keeps
system certificate verification unless `--allow-insecure-tls` is supplied.

## Mutation

- Multipart upload sends the HPM as the UpdateFile part and defaults its documented
  UpdateParameters to `ForceUpdate=true` and `ActiveMode=ResetBMC`. This permits a
  deliberate same-version reflash and gives Fresh Runtime qualification an observable
  reboot boundary. Callers may override only the supported typed values.
- HttpPush uploads the HPM body to the target-advertised URI.
- SimpleUpdate posts an explicitly supplied, BMC-reachable ImageURI; it does
  not upload a local file.

Use the normal Redfish timeout for discovery and status reads. Use the separate
`upload_timeout` for MultipartHttpPushUri and HttpPushUri byte transfer; its
default is 600 seconds and the task deadline remains the outer bound. A lost
transport response must report the selected path, request byte count, timeout,
and exception type without including credentials or artifact content.

Capture the returned Location, TaskMonitor, or task URI. Retain the Manager firmware
version and `LastResetTime` observed before upload when available. Do not retry an
ambiguous request before checking that resource.

## Monitoring

Poll the returned task or monitor URI until a terminal state. Handle a reboot
window by reconnecting and checking the same task or installed version. Treat
Completed as necessary but not sufficient: re-read the installed version and Manager
`LastResetTime`. A Fresh Runtime release qualification requires the post-upgrade reset
time to be strictly newer than the pre-upload value; target epoch alone does not prove
a reboot.
Invoke openubmc-debug afterward only when the caller requested runtime
acceptance.

After an observed activation disconnect, correlate the Manager version with
the target's FirmwareInventory and pending UpdateService work. If the old
version is ActiveBMC, the requested version remains only in AvailableBMC, and
there is no pending task or firmware-to-take-effect entry, report an activation
fallback. This is a completed failed outcome, not a reason to repeat the
upload. Persist it as a terminal failed verification: a replay of the same
operation returns the same activation-fallback outcome without uploading, and
the completed journal does not block a later, separately identified operation
on the target. Keep a plain old-version observation retryable when the target
does not provide enough inventory or pending-work evidence to classify it.

For an upload that failed after the effect boundary, run the same read-only
classification before reopening the local artifact. If the expected version is
neither installed nor present in inventory and no activation work is pending,
transition the journal to `replan_required` and return that result without an
upload. This recovery remains possible when the temporary HPM file is gone;
any later deliberate replan must provide and re-hash the artifact again.

The Target Runtime transaction keeps the Upgrade Redfish Session isolated from
other domains. After activation, it advances the target epoch and reopens the
Upgrade Session before reading the installed version. Any optional Debug
acceptance must run through the same fresh-verification context and cannot use
pre-upgrade evidence.

## Failure

Stop on TLS, authentication, discovery, hash, or target-origin failure. Do not
fall back to SSH, Telnet, a copied upload URI, or a second upload. Rollback
needs a separate explicit authorization and artifact identity.
