# Bundling the default RepeaterUI

The UI source lives in [openHop_RepeaterUI](https://github.com/openhop-dev/openHop_RepeaterUI).
The backend distribution includes its **compiled production output** under
`repeater/web/html`. Native `pip` installs, Debian packages and Docker builds package
those files; they do not fetch or compile the standalone UI automatically.

**Merging a UI-source PR alone does not update the dashboard delivered by a backend
upgrade.** Refresh the backend bundle in a packaging PR before distributing the
matching backend. Do not hand-edit minified JavaScript or develop UI source here.

## Build and stage a compatible UI

Use a clean standalone UI checkout at the reviewed commit compatible with this
backend. The current bundle records that immutable commit and its lockfile digest
in `repeater/web/html/frontend-build.json`. Node 22 and npm are maintainer build
tools only; repeaters do not need Node or a UI checkout to install the package.

From the backend checkout:

```bash
BACKEND=$(pwd)
UI_SOURCE=/path/to/clean/openHop_RepeaterUI

# Confirm the intended source commit and clean working tree before building.
git -C "$UI_SOURCE" rev-parse HEAD
git -C "$UI_SOURCE" status --short

# Intentionally replaces the backend's tracked compiled UI, not a running install.
(
    cd "$UI_SOURCE"
    npm ci
    npm run type-check
    npm run test:unit -- --run
    npm run build-only -- --outDir "$BACKEND/repeater/web/html"
)
python scripts/check_frontend_bundle.py --record-source "$UI_SOURCE"
python -m pytest -q tests/test_bundled_frontend.py tests/test_native_updater_security.py
```

Vite empties the output directory. Use only the intended backend packaging tree,
not another developer's checkout or a live installation. Review and include the
**whole generated output**, including removal of obsolete hashed chunks, in the
backend packaging PR. The UI source checkout must remain clean; recording
provenance does not compile it or independently prove how output was produced.

## Verify the distribution, not just the source

`python scripts/check_frontend_bundle.py` verifies source provenance format, the
complete generated-file SHA-256 inventory and the entry assets referenced by the
index. It needs neither Node nor network access. A different bundle directory can
be checked with `--root /path/to/staged/repeater/web/html`.

The normal pytest suite also verifies that the reachable bundled JavaScript contains
the System Stats usage feature. The existing real-wheel regression now requires
**every** generated file and the provenance manifest in a newly built wheel, with
identical digests and no obsolete assets. Do not skip that test because source
component tests passed.

Build release artifacts from a clean staging directory without previous `build/`
or egg-info output. Install the resulting wheel into an isolated target and check
its installed `repeater/web/html`, then exercise System Stats in a production
browser. An existing installation can contain leftover files from old manual
trials; the inventory checker deliberately reports those rather than deleting
unrecognized files automatically.

These checks bind a backend release to an explicit UI build. They do not claim to
follow future UI `dev` changes automatically or cryptographically attest the build.
