"""Register-map field variable expansion.

Takes a ``field_vars`` config section that describes how to dissect a
contiguous block of registers (e.g. an EIP assembly) and expands each
register-local spec string into a flat ``EIPVariable`` with an absolute
assembly offset.

Example field_vars config::

    field_vars:
      EIP.Input:
        base_register: 18178
        register_width_bits: 16
        fields:
          - 18178:
              - Bit0.0:heartbeat
              - Bit0.1:machine_ready
          - 18200:
              - Chars0.32:program
"""

from __future__ import annotations

from typing import Any

from .variable import EIPVariable, Variable


def expand_field_vars(field_vars_cfg: dict[str, Any]) -> list[EIPVariable]:
    """Expand a ``field_vars`` config into a flat list of :class:`EIPVariable`.

    Each assembly key (e.g. ``"EIP.Input"``) gets a ``base_register``,
    ``register_width_bits``, and a list of ``fields``.  Every field entry is
    a single-key dict ``{register_number: [spec_string, ...]}``.

    The register number is used to compute the absolute byte offset within
    the assembly:

        register_step = register_width_bits // 8
        register_base_offset = (register_number - base_register) * register_step

    Each spec string uses register-local byte offsets (``Bit0.0``, ``Chars0.32``)
    that get shifted by the register base offset.

    Returns expanded variables in input order.
    """
    result: list[EIPVariable] = []

    for assembly_key, assembly_cfg in field_vars_cfg.items():
        if not isinstance(assembly_cfg, dict):
            raise ValueError(
                f"Expected dict for {assembly_key!r} field_vars config, "
                f"got {type(assembly_cfg).__name__}"
            )

        base_register = assembly_cfg.get("base_register")
        register_width_bits = assembly_cfg.get("register_width_bits")
        fields = assembly_cfg.get("fields", [])

        if not isinstance(base_register, int) or base_register < 0:
            raise ValueError(
                f"base_register must be a non-negative integer, "
                f"got {base_register!r}"
            )
        if (
            not isinstance(register_width_bits, int)
            or register_width_bits <= 0
            or register_width_bits % 8 != 0
        ):
            raise ValueError(
                f"register_width_bits must be a positive multiple of 8, "
                f"got {register_width_bits!r}"
            )
        if not isinstance(fields, list):
            raise ValueError(
                f"fields must be a list, got {type(fields).__name__}"
            )

        step = register_width_bits // 8

        for field_entry in fields:
            if not isinstance(field_entry, dict) or len(field_entry) != 1:
                raise ValueError(
                    "Each field entry must be a dict with a single key "
                    "(register number)"
                )

            register_number = next(iter(field_entry.keys()))
            specs = field_entry[register_number]

            if not isinstance(register_number, int):
                raise ValueError(
                    f"Register number must be an integer, "
                    f"got {register_number!r}"
                )
            if register_number < base_register:
                raise ValueError(
                    f"Register {register_number} is less than "
                    f"base_register {base_register}"
                )
            if not isinstance(specs, list):
                raise ValueError(
                    f"Field specs for register {register_number} "
                    f"must be a list"
                )

            register_base_offset = (register_number - base_register) * step

            for spec_item in specs:
                if not isinstance(spec_item, str):
                    raise ValueError(
                        f"Variable spec must be a string, "
                        f"got {spec_item!r}"
                    )

                if ":" in spec_item:
                    spec_part, label = spec_item.split(":", 1)
                else:
                    spec_part = spec_item
                    label = None

                full_spec = f"{assembly_key}.{spec_part}"

                try:
                    parsed = Variable.parse(full_spec, label=label)
                except ValueError as e:
                    raise ValueError(
                        f"Error parsing field var {full_spec!r} "
                        f"at register {register_number}: {e}"
                    ) from e

                if not isinstance(parsed, EIPVariable):
                    raise ValueError(
                        f"Expected EIPVariable from spec {full_spec!r}, "
                        f"got {type(parsed).__name__}"
                    )

                new_offset = parsed.offset + register_base_offset

                expanded = EIPVariable(
                    assembly=parsed.assembly,
                    type=parsed.type,
                    offset=new_offset,
                    extra=parsed.extra,
                    label=parsed.label,
                    byte_order=parsed.byte_order,
                )
                result.append(expanded)

    return result
