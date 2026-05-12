# Catalog evolution

Phase 6 formalizes how the AGTP method catalog evolves over time. The earlier phases shipped a static `core/methods.json` with a `version` field that nothing read. This document describes the runtime, tooling, and operational story now that the field has teeth.

## Versioning

The catalog uses **semantic versioning** (`MAJOR.MINOR.PATCH`):

| Bump | Meaning | Example |
|---|---|---|
| `PATCH` (1.0.0 → 1.0.1) | Description / category metadata changes only. No verbs added or removed. | Refining a verb's description, adding a category to an existing entry. |
| `MINOR` (1.0.0 → 1.1.0) | Verbs added. Existing verbs unchanged. May introduce new deprecation flags on existing verbs. | Adding `RECONCILE` to the catalog; flagging `AUDIT_LEGACY` as deprecated. |
| `MAJOR` (1.0.0 → 2.0.0) | Verbs removed or renamed. Servers and endpoints that referenced removed verbs must migrate before upgrading. | Removing `AUDIT_LEGACY`; renaming `LIST` to `ENUMERATE`. |

The version is in `core/methods.json` at the document root and is read by:

- `core.methods.catalog_version()` — programmatic access.
- `core.methods.catalog_versions_supported()` — list of versions this implementation can validate against (single-version today; multi-version is future work).
- The server manifest's `catalog_version` and `catalog_versions_supported` fields — exposed on every `DISCOVER` so clients can compare.

## Per-verb deprecation

Verbs in the catalog can carry deprecation metadata:

```json
"AUDIT_LEGACY": {
  "categories": ["analysis"],
  "description": "Performs the legacy audit. Use AUDIT instead.",
  "deprecated_in": "1.1.0",
  "removed_in": "2.0.0",
  "successor": "AUDIT"
}
```

Three optional fields:

| Field | Type | Meaning |
|---|---|---|
| `deprecated_in` | semver string | The catalog version that flagged this verb as deprecated. |
| `removed_in` | semver string or omitted | The version this verb is scheduled to disappear in. Servers MAY refuse to register endpoints declaring it after this version ships. |
| `successor` | verb name or omitted | The recommended replacement. Clients SHOULD surface this to users. |

A deprecated verb is **still admitted** by `is_approved_verb`. Deprecation does not remove a verb from the catalog — it flags it. Removal happens when the verb's entry disappears in a later (typically major) version.

The deprecation lifecycle:

1. **`MINOR` bump deprecates the verb.** `deprecated_in` is set to the new version. `removed_in` (typically the next major) and `successor` (the replacement) are populated. The verb keeps working; the dispatcher stamps `AGTP-Catalog-Warning` on responses.
2. **Operators migrate.** They run `agtp-catalog-diff` against their deployment, see the deprecated-verb references, update endpoint TOMLs / recipes / `[policies.methods]` to use the successor.
3. **`MAJOR` bump removes the verb.** The verb's entry disappears from `methods.json`. Endpoints / recipes still referencing it fail registration with structured errors; the synthesis runtime invalidates active plans referencing it at startup.

## Authoring a deprecation

Edit `scripts/methods_source.py` and add a `DEPRECATED` dict alongside the existing `METHODS` list:

```python
DEPRECATED = {
    "AUDIT_LEGACY": {
        "deprecated_in": "1.1.0",
        "removed_in": "2.0.0",
        "successor": "AUDIT",
    },
}
```

Run `python scripts/build_methods.py` to regenerate `core/methods.json`. The build script merges the deprecation metadata into the method's entry; methods without a `DEPRECATED` entry are unchanged.

## Runtime behavior

### Dispatcher: `AGTP-Catalog-Warning` header

When a request invokes a deprecated verb, the dispatcher stamps an advisory header on the response:

```
AGTP-Catalog-Warning: deprecated; successor=AUDIT; removed_in=2.0.0
```

Fields after `deprecated` are omitted when the catalog doesn't declare them. The header is **purely advisory** — the request still processes normally. The CLI and the Elemen drawer surface the header to the user.

### CLI: yellow warning before the body

```
$ agtp agtp://server AUDIT_LEGACY -d '{"subject":"x"}'
WARNING: AUDIT_LEGACY is deprecated. Successor: AUDIT. Removed in: 2.0.0.
{
  "result": "..."
}
```

The warning goes to stderr so transcripts and pipes capture it without garbling JSON.

### Drawer: italic + deprecated pill in autocomplete

The Compose drawer's verb autocomplete renders deprecated entries in italic + a small "DEPRECATED" pill. Hovering the row surfaces the full `deprecated_in` / `successor` / `removed_in` detail in a tooltip. The verb is still pickable; the visual treatment is the migration prompt.

### `register_custom` and `@method`: graceful skip

Custom-method registrations that name a verb the catalog has removed used to be a noisy boot failure. Phase 6 changes this:

```python
@method(name="LEGACY_AUDIT", ...)   # not in catalog any more
def handle_legacy_audit(req, st, doc):
    ...
```

emits a `CatalogWarning` to stderr and returns the function unmodified — the registration is silently skipped. The server boots; the method just isn't reachable. This lets a server upgrade its catalog and reload without crashing the boot sequence; the operator sees the warning in their logs and updates the registration on their schedule.

### `[policies.methods]`: skip unknown-verb entries

Entries under `[policies.methods]` in `agtp-server.toml` (`allow`, `disallow`, redirect endpoints) that name a verb not in the current catalog are skipped at config-load time with a `CatalogWarning`:

```
[server] agtp-server.toml: policies.methods.allow 'LEGACY_AUDIT' references a verb not in the current catalog. Entry skipped.
```

The remaining valid entries become the live policy; the boot sequence continues.

`disallow` admits legacy HTTP names (`GET`, `POST`, `PUT`, `DELETE`, `PATCH`) alongside catalog verbs because operators routinely write `disallow = ["PATCH"]` to override a wildcard `legacy` opt-in.

`legacy` enforces a strict 5-name set; an unknown legacy name is a typo and surfaces as a hard `ValueError` at boot.

### Synthesis runtime: startup invalidation

If an in-memory synthesis plan references a verb the catalog has removed, executing it would fail mid-walk (after step N-1's side effects). To prevent that, the runtime walks every active plan at startup and expires any whose steps reference removed verbs:

```
[server] synthesis syn-aBcdEfghIjkL... expired (catalog-evolution-removed-verb)
[server] catalog-evolution invalidation expired 1 synthesis/syntheses referencing removed verbs
```

The expiration is logged with a structured `reason` tag so the operator can correlate it with the catalog change. Subsequent invocations of the expired synthesis_id return `404 synthesis-not-found`.

## Tooling: `agtp-catalog-diff`

The diff CLI compares two catalogs and optionally scans a deployment for breakage.

### Pure catalog diff

```bash
$ agtp-catalog-diff old.json new.json
Catalog diff: 1.0.0 -> 1.1.0

Added (3 verbs):
  ATTEST
  FORECAST
  RECONCILE

Removed (1 verb):
  LEGACY_AUDIT

Newly deprecated (1 verb):
  AUDIT_LEGACY (successor: AUDIT, removed_in: 2.0.0)

Summary: no breaking changes detected.
```

### Deployment-aware diff

Pass `--against-deployment <dir>` to scan a deployment's `endpoints/`, `agtp-recipes.toml`, and `agtp-server.toml`'s `[policies.methods]` block for references to removed verbs and paths that collide with newly-added verbs:

```bash
$ agtp-catalog-diff old.json new.json --against-deployment ./agtp-server/
Catalog diff: 1.0.0 -> 1.1.0

Added (1 verb):
  FORECAST

Removed (1 verb):
  LEGACY_AUDIT

Path-grammar conflicts (1 endpoint TOML reference paths that contain newly-added verbs):
  endpoints/forecast.toml  (path /forecast/{id} contains FORECAST)

Endpoint conflicts (1 endpoint TOML declare removed verbs):
  endpoints/audit.toml: method = LEGACY_AUDIT

Recipe conflicts (1 recipe step reference removed verbs):
  agtp-recipes.toml: recipe 'audit-flow' step 2: LEGACY_AUDIT

Method-policy conflicts (1 entry in [policies.methods] reference removed verbs):
  agtp-server.toml: allow: LEGACY_AUDIT

Summary: 4 breaking changes in deployment context.
```

### Exit codes

| Code | Meaning |
|---|---|
| `0` | No breaking changes detected. (Pure diff: always 0 unless a parse error occurred. Deployment scan: 0 only when nothing in the deployment references removed verbs and no path collides with a newly-added verb.) |
| `1` | Breaking changes detected in deployment context. |
| `2` | Parse error in either catalog file. |

### `--json` for CI

```bash
$ agtp-catalog-diff old.json new.json --json
{
  "old_version": "1.0.0",
  "new_version": "1.1.0",
  "added": ["FORECAST"],
  "removed": ["LEGACY_AUDIT"],
  "newly_deprecated": [...],
  "path_grammar_conflicts": [...],
  "endpoint_conflicts": [...],
  ...
}
```

Wire it into CI as a gate on catalog changes:

```yaml
# .github/workflows/catalog.yml
- run: agtp-catalog-diff catalog/old/methods.json core/methods.json --against-deployment .
```

## Cross-server version negotiation

The manifest exposes the server's catalog version on every DISCOVER:

```json
{
  "agtp_version": "1.0",
  "catalog_version": "1.1.0",
  "catalog_versions_supported": ["1.1.0"],
  ...
}
```

Clients SHOULD compare their local `catalog_version` to the server's on first DISCOVER:

- **Same version** — no notice required.
- **Same major, different minor** — the client may be authoring against a vocabulary the server hasn't seen yet (or vice versa); surface as an info-level notice.
- **Different major** — the client and server speak different vocabularies. Surface as a warning. Verbs the client uses that aren't in the server's catalog will get a 459 catalog refusal at first traffic.

This is advisory; the server's catalog is authoritative for validation. Phase 6 ships single-version support — `catalog_versions_supported` is exactly `[catalog_version]`. Multi-version (a server that validates against multiple catalog versions simultaneously during a migration) is future work; the field rides on the wire now so clients can read it without breaking when that capability lands.

## Operator runbook: deploying a new catalog

The recommended sequence:

1. **Pre-flight diff.** Run `agtp-catalog-diff <old> <new> --against-deployment <your-server-root>` against your live deployment. Anything in the output's "Endpoint conflicts" / "Recipe conflicts" / "Methods.txt conflicts" sections is a registration failure waiting to happen.
2. **Migrate references.** For each conflict, update the offending file. Use the deprecation metadata's `successor` field as a starting point.
3. **Verify the diff is clean.** Re-run the diff. It should now report only "Added" / "Newly deprecated" sections.
4. **Deploy the new catalog.** Replace `core/methods.json` (or the equivalent install path).
5. **Restart the server.** Watch the boot logs for:
   - `[server] catalog-evolution invalidation expired N synthesis/syntheses` — in-memory plans referencing removed verbs were cleaned up.
   - `[server] agtp-server.toml: policies.methods.allow 'X' references a verb not in the current catalog. Entry skipped.` — `[policies.methods]` entries skipped (probably leftovers from an old catalog).
   - `CatalogWarning: Custom method 'X' references a verb not in the current catalog. Registration skipped.` — a `@method` decorator referencing a removed verb.
6. **Test traffic.** Probe the deprecated verbs with `agtp <uri> X --grammar-check` and confirm the CLI surfaces the `AGTP-Catalog-Warning` header.

## Rolling back

Catalog versioning is monotonic in spirit but rollback is mechanical: replace `core/methods.json` with the older version and restart. No state migration. The synthesis runtime's invalidation pass expires plans on the way down too — a plan instantiated under the new catalog whose recipe references a verb that doesn't exist in the old catalog gets cleaned up on restart.

## Future work (out of scope for Phase 6)

- **Endpoint registry `--dry-run` boot mode.** Useful for "would this catalog change break my endpoints?" without deploying. The diff CLI catches the same issues with broader scope; the dry-run boot is incremental.
- **Drawer library invalidation.** When a drawer's `localStorage` library entry references a removed verb, the drawer doesn't actively clean it up. Saving / submitting the entry will fail; the user has to delete it manually.
- **Cross-server adaptation beyond manifest exposure.** Clients that automatically rewrite or downgrade requests based on the server's catalog version are future work.
- **Catalog publishing infrastructure.** The catalog lives in `core/methods.json` in the repo today. Formal publishing (release tags, signing, distribution) is governance, separate from runtime mechanics.
- **Multi-version catalog support.** A server that can validate against multiple catalog versions simultaneously. The `catalog_versions_supported` field is in the manifest as future-proofing.
