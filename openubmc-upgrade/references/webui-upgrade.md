# WebUI BMC firmware upgrade

The WebUI path uses the target's Redfish credential pair to log in to `/UI/Rest/Login`, obtains its cookie and CSRF token, and reads the existing firmware tasks before upload. An active pre-existing task blocks a new upload. Authentication values stay in memory and are absent from receipts.

Runtime uploads the verified HPM once as multipart to `/UI/Rest/FirmwareInventory`, starts the returned same-origin `/tmp/web/<filename>` through `/UI/Rest/BMCSettings/UpdateService/FirmwareUpdate`, and monitors `UpdateProgress`. A numeric task ID binds task reads; without one, only newly observed identities matching the uploaded filename count. Old or unrelated completed tasks cannot complete this operation. Every selected task must be Completed with ErrorCode zero.

Task completion is followed by a fresh Redfish Manager version read. A lost activation response or progress connection permits read-only version polling, never another upload. An upload connection loss remains uncertain in the existing mutation journal. An explicit HTTP 4xx upload rejection is eligible for replanning; rejection or loss after activation does not establish rollback.

The client rejects cross-origin redirects, uses an explicit Content-Length, and attempts to delete its WebUI session through the same target's Redfish SessionService. Cleanup evidence is retained separately from firmware verification.

The WebUI client comes from openUBMC plugin 2.1.6 source `cc03bc407e41320e7b40a33023ad7b6a96cb095d`; this integration retains the existing Runtime operation and journal APIs. BMC firmware requires Manager version verification. No independent firmware rollback API is introduced: installing a separately qualified recovery HPM is a distinct authorized upgrade operation.
