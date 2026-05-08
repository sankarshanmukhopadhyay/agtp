"""
Synthesis contract validation.

When PROPOSE returns 200 with a Synthesis, the synthesis spec should
itself satisfy the AMG grammar. This module wraps the validator with
the additional checks that synthesis introduces:

  * the proposed method passes standard AMG validation;
  * every target method exists on the server;
  * the parameter mapping covers every required parameter;
  * the synthesis is non-cyclic (no method maps to itself).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from client.amg.grammar import AMGMethodSpec
from client.amg.reserved import EMBEDDED_METHODS
from client.amg.validator import ValidationError, ValidationResult, validate


@dataclass
class SynthesisContract:
    """
    A bound synthesis: a proposal a server has agreed to honor by
    composing one or more underlying methods.

    Returned by PROPOSE 200 responses, then named via the
    ``Synthesis-Id`` header on follow-up requests.
    """

    synthesis_id: str
    proposed_method: AMGMethodSpec
    target_methods: List[str]
    parameter_mapping: Dict[str, str]    # proposal-param -> target-param
    expected_output: Dict[str, Any] = field(default_factory=dict)
    expires_at: Optional[str] = None     # ISO 8601


def validate_synthesis(
    contract: SynthesisContract,
    server_methods: Set[str],
) -> ValidationResult:
    """
    Validate a synthesis contract against the AMG grammar plus the
    integrity rules above.

    ``server_methods`` is the set of methods reachable on the server
    that issued the synthesis. It must include every target method
    named in the contract.
    """
    server_methods = {m.upper() for m in server_methods} | set(EMBEDDED_METHODS)

    # 1. Run the standard validator on the proposed method. We treat
    #    the union (server_methods + EMBEDDED_METHODS) as the known
    #    universe for substitution checks so the proposed method can
    #    name any reachable target.
    result = validate(
        contract.proposed_method,
        known_methods=server_methods,
    )
    if not result.valid:
        return result

    spec = contract.proposed_method

    # 2. Empty target_methods is rejected: a synthesis that points at
    #    nothing has nothing to dispatch to.
    if not contract.target_methods:
        return _refuse(
            spec.name,
            "synthesis-empty-targets",
            "synthesis declares no target_methods",
            pass_name="synthesis",
        )

    # 3. Every target method must be reachable on the server.
    for target in contract.target_methods:
        upper = (target or "").upper()
        if not upper:
            return _refuse(
                spec.name,
                "synthesis-empty-target",
                "synthesis target_methods entry is empty",
                pass_name="synthesis",
            )
        if upper == spec.name.upper():
            return _refuse(
                spec.name,
                "synthesis-cycle",
                (
                    f"synthesis target {upper!r} matches the proposed "
                    f"method's own name; cycles are forbidden"
                ),
                pass_name="synthesis",
            )
        if upper not in server_methods:
            return _refuse(
                spec.name,
                "synthesis-unknown-target",
                (
                    f"synthesis target {upper!r} is not a method this "
                    f"server exposes"
                ),
                pass_name="synthesis",
            )

    # 4. parameter_mapping must cover every required parameter of the
    #    proposed method.
    declared = {p.name for p in spec.required_params or []}
    mapped = set(contract.parameter_mapping.keys())
    missing = sorted(declared - mapped)
    if missing:
        return _refuse(
            spec.name,
            "synthesis-mapping-incomplete",
            (
                f"parameter_mapping omits required parameter(s): "
                f"{', '.join(missing)}"
            ),
            pass_name="synthesis",
            suggestion=(
                "Map every required parameter of the proposed method to a "
                "parameter of one of the target methods."
            ),
        )

    # All checks passed. Append the synthesis pass to the existing
    # result so callers see the full chain.
    result.passes.append(_synthesis_ok(contract))
    return result


def _refuse(
    method_name: str,
    code: str,
    message: str,
    *,
    pass_name: str,
    suggestion: Optional[str] = None,
) -> ValidationResult:
    err = ValidationError(
        pass_name=pass_name,
        code=code,
        message=message,
        suggestion=suggestion,
    )
    return ValidationResult(
        valid=False,
        method_name=method_name,
        passes=[],
        error=err,
    )


def _synthesis_ok(contract: SynthesisContract):
    """Build the final pass-result entry for a successful synthesis."""
    from client.amg.validator import PassResult
    return PassResult(
        name="synthesis",
        passed=True,
        detail=(
            f"synthesis_id={contract.synthesis_id} "
            f"target_methods={','.join(contract.target_methods)}"
        ),
    )


__all__ = [
    "SynthesisContract",
    "validate_synthesis",
]
