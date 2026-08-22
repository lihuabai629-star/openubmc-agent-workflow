from __future__ import annotations

from pathlib import Path
import sys
import time


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    CredentialResolver,
    CredentialSelector,
    MutationAuthorization,
    MutationJournalStore,
    MutationRequest,
    OpenUBMCTaskRun,
    RemoteReadRequest,
    ResolvedSshCredentials,
    TargetSpec,
)


def pause(marker: Path) -> None:
    marker.write_text("ready\n", encoding="utf-8")
    while True:
        time.sleep(1)


def main() -> None:
    root = Path(sys.argv[1])
    cut = sys.argv[2]
    marker = root / "marker"
    counter = root / "apply-count"
    selector = CredentialSelector.for_ssh(
        user="root",
        user_env="",
        password_env="",
        identity_file="",
        environ={},
    )
    target = TargetSpec(
        host="bmc.example",
        credential_selector_fingerprint=selector.fingerprint,
    )
    task = OpenUBMCTaskRun(
        task_id="crash-task",
        credential_resolver=CredentialResolver(
            lambda _selector: ResolvedSshCredentials(user="root", password="test")
        ),
        mutation_journal_store=MutationJournalStore(root / "journals"),
    )
    request = MutationRequest.create(
        operation_id="crash-effect-1",
        target=target,
        credential_selector=selector,
        action="live_patch",
        operation={
            "remote": "/opt/bmc/apps/demo/unit.lua",
            "sha256": "b" * 64,
        },
    )
    read = RemoteReadRequest.create(
        request_id="crash-verify",
        target=target,
        credential_selector=selector,
        collector_name="crash-cut",
        operation={"request": "crash-verify"},
    )

    def apply(context):
        count = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
        counter.write_text(str(count + 1), encoding="utf-8")
        context.mark_effects_started()
        if cut == "effect_started":
            pause(marker)
        return {
            "remote_after_sha256": "b" * 64,
            "root_mount_restored": True,
        }

    def verify(context):
        if cut == "result_persisted":
            pause(marker)
        return context.run_read(
            read,
            lambda read_context: read_context.epochs.target_epoch,
        )

    task.run_mutation(
        request,
        authorization=MutationAuthorization.from_task_intent(
            "diagnose-and-fix",
            delivery_strategy="live-patch",
        ),
        apply=apply,
        verify=verify,
    )
    if cut == "terminal_committed":
        pause(marker)
    raise ValueError(f"unsupported crash cut: {cut}")


if __name__ == "__main__":
    main()
