# Domain Pack 作者契约

Domain Pack 是 Runtime Core 内部的领域扩展 Module。作者只需要声明一个
`DomainPackAuthorContract`；契约携带 `CapabilityDescriptor`，Runtime 负责合并 registry、
校验 Pack-set 一致性、应用 Effect 策略，并把有界 conformance 摘要投影到 Operator 状态。

Domain Pack 不会增加 Agent-facing operation。默认 Agent Interface 始终只有
`observe` 与 `execute`。

## READ_ONLY Pack

```python
contract = DomainPackAuthorContract(
    descriptor=CapabilityDescriptor(
        operation="log_bundle_index",
        capability="openubmc.logs.index",
        owner_skill="openubmc-log-analyzer",
        input_schema={"type": "object", "additionalProperties": True},
        output_schema={"type": "object", "additionalProperties": True},
        timeout_seconds=120,
        evidence_types=("diagnostic-bundle-index",),
        effect_class=EffectClass.READ_ONLY,
    ),
    name="log-bundle-index",
    version="1",
    effect_class=EffectClass.READ_ONLY,
    adapter=index_adapter,
    verifier=lambda action, receipt: (
        result_contract.bind(action, receipt) is not None
    ),
    conformance_example=DomainPackConformanceExample(
        arguments={
            "ip": "conformance-target",
            "artifact_ref": bundle_ref,
        },
        receipt=index_receipt,
    ),
    artifact_contract=ArtifactContract(
        path_fields=("_artifact_path",),
        artifact_kind="openubmc-log-bundle",
        required=True,
        reference_required=True,
    ),
    result_artifact_contract=ResultArtifactContract(
        "openubmc-log-index",
    ),
)
```

READ_ONLY Pack 可以由 Runtime 做有限传输重试，但不能声明 reconciler 或 journal
action。需要红化输入时必须使用 ArtifactRef，不能依靠裸路径声明访问范围。

## RECONCILABLE_MUTATION Pack

```python
contract = DomainPackAuthorContract(
    descriptor=upgrade_descriptor,
    name="upgrade",
    version="1",
    effect_class=EffectClass.RECONCILABLE_MUTATION,
    adapter=upgrade_adapter,
    reconciler=upgrade_adapter,
    verifier=verify_upgrade_receipt,
    conformance_example=DomainPackConformanceExample(
        arguments=hermetic_upgrade_arguments,
        receipt=hermetic_verified_upgrade_receipt,
    ),
    journal_action=lambda arguments: "upgrade",
    artifact_contract=ArtifactContract(
        path_fields=("artifact_path",),
        digest_field="artifact_sha256",
        version_field="product_version",
        artifact_kind="openubmc-hpm",
        required=True,
    ),
    artifact_phase="build.artifact",
    closeout_stage="upgrade",
)
```

Mutation Pack 固定单次执行尝试。结果未知时只能由 reconciler 使用同一个 Effect identity
读取持久 journal 并收敛，不能创建替代 Effect。journal action 必须稳定且非空。

## 注册与一致性

扩展 callback 只能返回一个或多个 `DomainPackAuthorContract`，不能返回预构造的裸
`DomainPack`。每个作者契约必须携带纯数据的 `DomainPackConformanceExample`；该类型不接受
callback，因此 composition 检查不会执行 BMC、凭据、网络或文件 I/O。Runtime
composition 使用
`DomainPackConformanceSuite.bind()` 统一构造内建与扩展 Pack，并拒绝：

- operation 重复或同名同版本 Pack 重复；
- descriptor 的完整 Effect class 与 Pack 声明不一致；
- capability requirement 未注册；
- 多个 Pack 占用同一 Artifact phase；
- recovery、Artifact input/output 或 verifier 契约不完整。

composition 会强制运行每个契约的样例，通过 `verify_example()` 验证 Effect identity、
receipt operation、verifier、只读重试分类和 mutation recovery 分类。该验证不调用真实
BMC。

作者契约可以贡献静态 MCP definitions 中不存在的新 capability。Runtime 从 descriptor
生成内部 dispatch declaration，并把 operation owner 加入 WorkflowDefinitions；无需修改
MCP transport。若要从 Agent Interface 直接运行一个新的一阶段 READ_ONLY Pack，使用现有
`execute`：

```python
execute({
    "kind": "start",
    "target": "192.0.2.10",
    "intent": "diagnosis-only",
    "entry_operation": "hardware_health",
    "purpose": "read target health",
})
```

这不会增加新的 Agent-facing operation；工具面仍只有 `observe` 和 `execute`。此类入口应在
作者契约中声明 `closeout_stage`，让 Runtime 用对应阶段回执形成终态 Outcome。

## 版本与兼容规则

- `version` 使用数字语义版本，行为不兼容时升级 major；
- operation identity 和 capability identity 不能靠版本升级静默改义；
- 已持久 Run 继续由固定 definition/event reader 或显式 upcaster 读取；
- compatibility Adapter 可以翻译旧 receipt，但必须委托给同一 Domain Pack verifier；
- Pack 不得读取 MCP transport、Agent profile 或模型上下文来决定领域语义。

Operator `runtime_status` 返回 `domain_pack_conformance`，用于确认当前组合的 Pack 数量、
operation 列表和 READ_ONLY 重试参数。该信息不进入 Agent 默认工具面。
