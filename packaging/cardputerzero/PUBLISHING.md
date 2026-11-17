# Publishing openhop-cz to the CardputerZero app store

The CardputerZero store is an APT repository fed by pull requests to
[CardputerZero/packages](https://github.com/CardputerZero/packages). A
submission is made with `czdev publish`, from
[CardputerZero/AppBuilder](https://github.com/CardputerZero/AppBuilder). It:

1. checks the `.deb` (has a launcher `.desktop` entry, no systemd service
   running as root, version newer than the published one) and the store
   listing in `app-builder.json`;
2. forks `CardputerZero/packages` to the publishing GitHub account and uploads
   the `.deb` to a release named `czdev-buffer` on that fork;
3. opens a pull request on `CardputerZero/packages` with the listing
   (`meta.json`, icon, screenshots) and a manifest pointing at the `.deb`.

When the pull request is merged the store's CI copies the `.deb` into the APT
repository and the app appears in the store on the device.

## Things to know first

- **The package name belongs to the first GitHub account that publishes it.**
  Only that account can publish updates to `openhop-cz`. Decide which account
  owns it before the first release: a maintainer's, or a machine account the
  project controls. A machine account is the better fit for publishing from
  GitHub Actions.
- **The first release is reviewed by hand.** After the pull request is opened,
  post a short video of the app running on a real CardputerZero as a comment
  on it. A store maintainer merges it after watching.
- **Updates merge automatically** once the store's checks pass, which is what
  makes publishing from a workflow practical.
- The store title (`openHop Repeater`) and share code (`HOP1`) must be unique
  in the store. If either is taken, change it in `app-builder.json`.

## First release, by hand

On a computer with Python 3, git and `dpkg-deb` (macOS: `brew install dpkg`):

1. Get the `.deb`: download it from the `CardputerZero Package` workflow run
   of the release (artifact `openhop-cz-deb`, also attached to the GitHub
   release), or build it as described in [README.md](README.md). Put it in
   `packaging/cardputerzero/build/`.
2. Install it on a CardputerZero and check it: the repeater starts from the
   app, the dashboard opens, and a packet is repeated.
3. Get the store tool and sign in as the account that will own the package:

   ```bash
   git clone https://github.com/CardputerZero/AppBuilder.git
   AppBuilder/czdev login
   ```

4. Publish from the directory that holds `app-builder.json`:

   ```bash
   cd packaging/cardputerzero
   ../../AppBuilder/czdev publish --deb build/openhop-cz_<version>_arm64.deb
   ```

   `czdev` pushes over SSH (`git@github.com:`), so the account needs an SSH
   key on that computer.
5. Open the pull request it prints, and post the video.

A change to the listing only (summary, screenshots, icon) does not need a new
`.deb`: run `czdev publish --meta-only` from the same directory.

## Updates from GitHub Actions

`.github/workflows/cardputerzero-package.yml` builds the package on every
GitHub release and attaches it to the release. It can also submit it to the
store. That part is off until it is configured:

1. Create a **classic** personal access token for the account that owns the
   package, with the `public_repo` scope only. (A fine-grained token cannot
   fork `CardputerZero/packages` or open a pull request on it.)
2. In the repository settings, create an environment named
   `cardputerzero-store` and add the token to it as the secret
   `CZDEV_GITHUB_TOKEN`. Add required reviewers to the environment if a
   person should approve each submission.
3. Add the repository variable `CARDPUTERZERO_STORE_PUBLISH` with the value
   `true`.

From then on, publishing a GitHub release (not a pre-release) builds
`openhop-cz_<tag>-1_arm64.deb` and opens the store pull request. The workflow
can also be run by hand from the Actions tab, with a different `revision` to
republish the same openHop version after a packaging fix, and with
`publish_to_store` ticked to submit it.

The workflow writes the token where `czdev` expects its login
(`~/.czdev/credentials`) and makes git use it over HTTPS in place of SSH.
`czdev` has no documented CI mode, so this depends on how `czdev` works at the
commit pinned in the workflow (`APPBUILDER_REF`); review that when updating
the pin.

The store job refuses development versions (`~dev`), so only tagged releases
reach the store.

## Maintainer field

`build_deb.sh` writes `MAINTAINER` into the package (default: the maintainer
in `debian/control`). The workflow takes it from the repository variable
`CARDPUTERZERO_MAINTAINER` when that is set. The store shows the author from
`app-builder.json` (`store.author.display_name`) and records the publishing
account.
