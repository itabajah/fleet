"""Per-fleet repo bundles: named, ordered aliases for ``--repos`` lists.

Stored at ``<fleet-root>/bundles.json`` (sibling of ``fleet.json``).
Mirrors the :mod:`fleet.fleets_config` patterns: dataclass + atomic
tmp+``os.replace`` writes + reuse of :func:`_config_lock` for
cross-process serialisation.

A bundle is a flat, ordered list of repo tokens — exactly what
:func:`fleet.tasks.validation.resolve_repo` accepts. Bundles cannot
contain other bundles: any token starting with ``@`` is rejected at
write time, which makes circular references structurally impossible.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from fleet.errors import FleetError
from fleet.fleets_config import _config_lock
from fleet.state import bundles_path

# Same shape as task / fleet names: alphanum start, then alphanum + ``.``
# ``_`` ``-``. Longer cap than task names (bundles are pure config, not a
# git ref).
_BUNDLE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")


def validate_bundle_name(name: str) -> None:
    """Raise :class:`FleetError` if ``name`` is not a valid bundle name."""
    if not _BUNDLE_NAME_RE.match(name):
        raise FleetError(
            f"Invalid bundle name '{name}'. "
            "Use letters, digits, '.', '_', '-' (1-64 chars, must start "
            "with a letter or digit)."
        )


def _validate_member_token(tok: str) -> None:
    if not tok:
        raise FleetError("Bundle member token cannot be empty.")
    if tok.startswith("@"):
        raise FleetError(
            f"Bundle member '{tok}' cannot start with '@' "
            "(bundles cannot contain other bundles)."
        )


@dataclass
class BundlesConfig:
    """Validated ``bundles.json`` contents."""

    bundles: dict[str, list[str]] = field(default_factory=dict)

    # ------------------------------ I/O --------------------------------------

    @classmethod
    def load(cls) -> BundlesConfig:
        path = bundles_path()
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as e:
            raise FleetError(f"Malformed bundles config at {path}: {e}") from e
        if not isinstance(data, dict):
            raise FleetError(
                f"Malformed bundles config at {path}: top-level value is "
                f"not an object."
            )
        raw = data.get("bundles")
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise FleetError(
                f"Malformed bundles config at {path}: 'bundles' must be "
                f"an object."
            )
        out: dict[str, list[str]] = {}
        for name, members in raw.items():
            if not isinstance(name, str) or not _BUNDLE_NAME_RE.match(name):
                raise FleetError(
                    f"Malformed bundles config at {path}: invalid bundle "
                    f"name {name!r}."
                )
            if not isinstance(members, list) or not all(
                isinstance(m, str) for m in members
            ):
                raise FleetError(
                    f"Malformed bundles config at {path}: bundle "
                    f"'{name}' must be a list of strings."
                )
            for m in members:
                if m.startswith("@"):
                    raise FleetError(
                        f"Malformed bundles config at {path}: bundle "
                        f"'{name}' contains nested reference '{m}'."
                    )
            out[name] = list(members)
        return cls(bundles=out)

    def save(self) -> None:
        path = bundles_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "bundles": {
                name: list(self.bundles[name])
                for name in sorted(self.bundles)
            }
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        try:
            os.replace(tmp, path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise

    # ------------------------------ accessors --------------------------------

    def names(self) -> list[str]:
        return sorted(self.bundles)

    def get(self, name: str) -> list[str]:
        if name not in self.bundles:
            known = ", ".join(self.names()) or "(none)"
            raise FleetError(
                f"No such bundle: '{name}'. Known bundles: {known}."
            )
        return list(self.bundles[name])

    # ------------------------------ mutators ---------------------------------

    def add(self, name: str, tokens: list[str], *, force: bool) -> list[str]:
        """Create (or overwrite with ``force``) bundle ``name``.

        Returns the deduped, ordered token list actually stored.
        """
        validate_bundle_name(name)
        if name in self.bundles and not force:
            raise FleetError(
                f"Bundle '{name}' already exists. Use --force to overwrite."
            )
        members = _dedup_tokens(tokens, label=f"bundle '{name}'")
        if not members:
            raise FleetError(
                f"Bundle '{name}' would be empty. "
                "Pass at least one repo token."
            )
        self.bundles[name] = members
        return list(members)

    def remove(self, name: str) -> None:
        if name not in self.bundles:
            known = ", ".join(self.names()) or "(none)"
            raise FleetError(
                f"No such bundle: '{name}'. Known bundles: {known}."
            )
        del self.bundles[name]

    def edit(self, name: str, add: list[str], remove: list[str]) -> list[str]:
        """Apply ``add`` / ``remove`` to bundle ``name``. Returns new members.

        ``add`` tokens are appended in order (dedup against existing
        members). ``remove`` tokens are dropped by exact match — tokens
        that aren't members are silently ignored here; callers are
        expected to warn the user (the handler does).
        """
        current = self.get(name)
        for tok in add:
            _validate_member_token(tok)
        remove_set = set(remove)
        kept = [t for t in current if t not in remove_set]
        for tok in add:
            if tok not in kept:
                kept.append(tok)
        self.bundles[name] = kept
        return list(kept)


def _dedup_tokens(tokens: list[str], *, label: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        _validate_member_token(tok)
        if tok in seen:
            print(f"note: ignoring duplicate '{tok}' in {label}")
            continue
        seen.add(tok)
        out.append(tok)
    return out


# ---------------------------------------------------------------------------
# Expansion seam (called by task new / add-repo / remove-repo)
# ---------------------------------------------------------------------------

def expand_bundle_tokens(tokens: list[str]) -> list[str]:
    """Expand ``@bundle`` references in ``tokens`` to their member lists.

    Non-``@`` tokens pass through unchanged. Order is preserved. The
    `BundlesConfig` is loaded lazily — and only once — so commands that
    don't reference any bundle pay no I/O cost.

    Raises :class:`FleetError` on unknown bundle references; deduping of
    the expanded list is left to the caller (which already runs its own
    ``seen``-set logic with friendlier per-repo messages).
    """
    if not any(t.startswith("@") for t in tokens):
        return list(tokens)
    cfg = BundlesConfig.load()
    out: list[str] = []
    for tok in tokens:
        if not tok.startswith("@"):
            out.append(tok)
            continue
        name = tok[1:]
        if not name:
            raise FleetError(
                "Empty bundle reference '@'. Use '@<bundle-name>'."
            )
        if name not in cfg.bundles:
            known = ", ".join(cfg.names()) or "(none)"
            raise FleetError(
                f"Unknown bundle '{tok}'. Known bundles: {known}."
            )
        out.extend(cfg.bundles[name])
    return out
