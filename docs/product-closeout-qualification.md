# Product closeout qualification

Product closeout is an Operator / CI Plane decision over immutable evidence. It does not add an
Agent operation and does not create a second Run authority.

The public seam is:

```bash
python3 scripts/product_closeout_qualification.py evidence.json \
  --output product-closeout-report.json
```

Fresh Runtime qualification additionally requires the Operator / CI Plane to select the trusted
Runtime ledger independently of the evidence manifest:

```bash
python3 scripts/product_closeout_qualification.py fresh-evidence.json \
  --runtime-repository /trusted/runtime-state/runtime.sqlite3 \
  --output product-closeout-report.json
```

The Operator / CI Plane can also assemble that manifest from trusted workflow outputs instead of
hand-authoring dimension status and identities:

```bash
python3 scripts/product_closeout_ingestion.py fresh-ingestion.json \
  --runtime-repository /trusted/runtime-state/runtime.sqlite3 \
  --output-manifest product-closeout-manifest.json \
  --output-report product-closeout-report.json
```

Continuous qualification accepts the same descriptor through `--product-ingestion`. Ingestion
derives the selected target and terminal Outcome from the Run ledger, source commits from clean Git
repositories, artifact identity from the adjacent build metadata, and status/digests from fixed
proof and support files. It does not write Runtime state: every proof and support digest must
already be attached before `RunOutcomeRecorded`.

The manifest separates nine dimensions: Runtime continuity, diagnosis, source identity, official
UT, compiled build, artifact identity, upgrade, freshness, and hardware coverage. Every referenced
file is verified by SHA-256. Fresh Runtime evidence uses the structured proof schema, but a proof
is accepted only when its digest and its fixed-format supporting evidence were attached to the
same persisted Runtime Run before `RunOutcomeRecorded`. The qualifier replays that SQLite ledger,
checks the target and completed Outcome, and verifies an immutable digest of the Run events. The
Operator-selected database and WAL are copied into a stable snapshot before replay, so
qualification never runs migrations or index backfills against the trusted ledger.
Historical manifests select one repository-owned `evidence_type`; they cannot supply executable or
declarative content claims. The built-in evidence types are:

| Evidence type | Dimension | Fixed verification |
| --- | --- | --- |
| `workflow-diagnosis-record` | diagnosis | diagnosis/root-cause and fix chain |
| `workflow-official-ut-record` | official UT | non-zero complete `N/N passed` results |
| `component-build-log` | build | package revision, matching full package reference, successful terminal state |
| `product-build-log` | build | HPM build, signing, and successful final task |
| `workflow-upgrade-record` | upgrade | upload/activation completion and installed artifact version |
| `reboot-acceptance-timeline` | freshness | manager readiness, final direct/RAID convergence, accepted elapsed time |
| `drive-summary-json` | hardware | direct attribution, RAID zero attribution, health, presence, serial, and scoped drive identities |

Every source repository must be clean at the exact recorded commit; the firmware artifact must
match its path, digest, size, and version. Hardware coverage is exact, so SATA or SAS evidence
cannot satisfy an NVMe case; the protocol is parsed from the drive evidence rather than accepted
from manifest device labels. Fresh evidence also binds upgrade completion, target observation,
hardware acceptance, and terminal Runtime Outcome into one ordered, timezone-aware timeline with a
declared maximum post-upgrade evidence age.

Two claim levels are intentionally different:

| Claim | Meaning | Promotable |
| --- | --- | --- |
| `historical-product-validated` | The recorded product evidence is internally consistent and verifies its original scope, but it predates a current Runtime Run identity. | No |
| `fresh-runtime-product-closed` | The same product dimensions are complete and are bound to a terminal current Runtime Outcome. | Yes |

A historical result is useful evidence, not a synthetic Runtime success. Re-running the qualifier
never fabricates a Run ID or rewrites the original target evidence.

## 630 NVMe reconstruction

The retained 630 NVMe case was rechecked from its original source repositories and evidence. The
four repositories are clean at their recorded commits; official UT recorded 363/363 and 1251/1251
passes; all component and product build evidence is present; the HPM is version `12.08.21.10`,
93,563,247 bytes, with SHA-256
`2fc339ddadc4fb4b1d550257f7f07986f290f562a964c50a535030f6b9ace987`; and post-reboot
hardware evidence shows all eight direct NVMe drives attributed while all eight RAID drives remain
unattributed as designed.

The resulting evidence digest is
`sha256:6c7a1389d5114a1f119a0b1c0549e12d9ed3e19df8cd48e16cc70d2df0b13733`.
Its claim is `historical-product-validated` and `promotable=false` because the case predates the
current Runtime and therefore has no current Run ID or terminal Outcome. The retained machine
inputs are [630-nvme-product-closeout-manifest.json](qualification/630-nvme-product-closeout-manifest.json),
and the retained machine report is
[630-nvme-product-closeout.json](qualification/630-nvme-product-closeout.json). The external replay
bundle remains immutable at the paths and digests recorded in the manifest; it is not replaced by
synthetic repository fixtures.

## Fresh product checkpoint

A fresh product promotion additionally needs an independently authorized target, a rollback
package, a new Runtime Run, official validation and build evidence, a digest-bound HPM, successful
upgrade, and fresh protocol-specific target acceptance. Repository CI deliberately requires no
BMC, credentials, private network, or upgrade authority.
