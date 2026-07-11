"""Deterministic Enrollment checks for configured Managed Applications."""

from app.domain.incidents import (
    ApplicationProfile,
    EnrollmentCheck,
    EnrollmentCheckCode,
    EnrollmentReadiness,
    EnrollmentSnapshot,
    ManagedWorkload,
    WorkloadReference,
)

ENROLLED_NAMESPACE_LABEL = "kubecouncil.io/enrolled"
MANAGED_WORKLOAD_LABEL = "kubecouncil.io/managed"


class EnrollmentChecker:
    """Evaluates typed read-only prerequisites without granting any authority itself."""

    def check(
        self, profile: ApplicationProfile, snapshot: EnrollmentSnapshot
    ) -> EnrollmentReadiness:
        namespaces = {state.namespace: state for state in snapshot.namespaces}
        workloads = {state.reference: state for state in snapshot.workloads}
        checks: list[EnrollmentCheck] = [
            EnrollmentCheck(
                code=EnrollmentCheckCode.PROFILE_VALID,
                passed=True,
                message="Application Profile passed typed validation.",
            )
        ]
        for namespace in profile.namespaces:
            namespace_state = namespaces.get(namespace)
            selected = namespace_state is not None and namespace_state.exists
            checks.append(
                EnrollmentCheck(
                    code=EnrollmentCheckCode.NAMESPACE_SELECTED,
                    passed=selected,
                    message=(
                        f"Namespace {namespace} is selected by Enrollment."
                        if selected
                        else f"Namespace {namespace} is missing from Enrollment inspection."
                    ),
                )
            )
            checks.append(
                EnrollmentCheck(
                    code=EnrollmentCheckCode.NAMESPACE_ENROLLED_LABEL,
                    passed=selected
                    and namespace_state is not None
                    and namespace_state.labels.get(ENROLLED_NAMESPACE_LABEL) == "true",
                    message=(
                        f"Namespace {namespace} has {ENROLLED_NAMESPACE_LABEL}: true."
                        if selected
                        and namespace_state is not None
                        and namespace_state.labels.get(ENROLLED_NAMESPACE_LABEL) == "true"
                        else f"Namespace {namespace} must have {ENROLLED_NAMESPACE_LABEL}: true."
                    ),
                )
            )
            investigator_binding = any(
                binding.exists
                and binding.namespace == namespace
                and binding.subject == profile.investigator_identity
                and binding.role == profile.investigator_role
                for binding in snapshot.role_bindings
            )
            executor_binding = any(
                binding.exists
                and binding.namespace == namespace
                and binding.subject == profile.executor_identity
                and binding.role == profile.executor_role
                for binding in snapshot.role_bindings
            )
            checks.extend(
                (
                    EnrollmentCheck(
                        code=EnrollmentCheckCode.INVESTIGATOR_ROLE_BINDING,
                        passed=investigator_binding,
                        message=(
                            f"Investigator RoleBinding is active in {namespace}."
                            if investigator_binding
                            else f"Investigator RoleBinding is missing in {namespace}."
                        ),
                    ),
                    EnrollmentCheck(
                        code=EnrollmentCheckCode.EXECUTOR_ROLE_BINDING,
                        passed=executor_binding,
                        message=(
                            f"Executor RoleBinding is active in {namespace}."
                            if executor_binding
                            else f"Executor RoleBinding is missing in {namespace}."
                        ),
                    ),
                )
            )
        for workload in profile.workloads:
            workload_state = workloads.get(workload.reference)
            selected = workload_state is not None and workload_state.exists
            checks.append(
                EnrollmentCheck(
                    code=EnrollmentCheckCode.WORKLOAD_SELECTED,
                    passed=selected,
                    message=(
                        f"Workload {workload.reference.name} is selected by Enrollment."
                        if selected
                        else (
                            f"Workload {workload.reference.name} is missing from "
                            "Enrollment inspection."
                        )
                    ),
                    workload=workload.reference,
                )
            )
            if workload.executable:
                managed = (
                    selected
                    and workload_state is not None
                    and workload_state.labels.get(MANAGED_WORKLOAD_LABEL) == "true"
                )
                checks.append(
                    EnrollmentCheck(
                        code=EnrollmentCheckCode.MANAGED_WORKLOAD_LABEL,
                        passed=managed,
                        message=(
                            f"Managed Workload {workload.reference.name} has "
                            f"{MANAGED_WORKLOAD_LABEL}: true."
                            if managed
                            else f"Managed Workload {workload.reference.name} must have "
                            f"{MANAGED_WORKLOAD_LABEL}: true."
                        ),
                        workload=workload.reference,
                    )
                )
        checks.append(
            EnrollmentCheck(
                code=EnrollmentCheckCode.ADMISSION_POLICY_BINDING,
                passed=snapshot.admission_policy_binding,
                message=(
                    "Admission-policy binding is active."
                    if snapshot.admission_policy_binding
                    else "Admission-policy binding is missing."
                ),
            )
        )
        return EnrollmentReadiness(
            application_id=profile.application_id,
            profile_version=profile.version,
            ready=all(check.passed for check in checks),
            checks=tuple(checks),
        )


def require_enrolled_target(
    profile: ApplicationProfile,
    readiness: EnrollmentReadiness,
    target: WorkloadReference,
    *,
    require_executable: bool = False,
) -> ManagedWorkload:
    """Return a declared target only after all Enrollment prerequisites are current."""

    if (
        readiness.application_id != profile.application_id
        or readiness.profile_version != profile.version
    ):
        raise ValueError("enrollment readiness does not match the application profile")
    if not readiness.ready:
        raise ValueError("Enrollment is not ready; targets remain outside KubeCouncil authority")
    workload = next((item for item in profile.workloads if item.reference == target), None)
    if workload is None:
        raise ValueError("target is outside the enrolled application profile")
    if require_executable and workload.protected_dependency:
        raise ValueError("protected dependency is observable but is never executable")
    if require_executable and not workload.executable:
        raise ValueError("target is not an executable Managed Workload")
    return workload
