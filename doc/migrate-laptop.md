# Move quant project to another computer

**Scope:** Atlas Lite + OCI access + Cursor history for this project + local `data/`.  
**Not in scope:** wiping the whole laptop or unrelated apps.

Secrets are **not** in Git. A ready-made pack is staged on OCI:

```text
opc@137.23.61.107:~/atlas-quant-migrate.tgz   (~277MB)
```

Contains: `kite_credentials`, `llm_credentials`, `atlas-oci.key.key`, Cursor project transcripts, local `data/`, docs.

---

## On the new computer (do this)

### A. Clone code

```bash
mkdir -p ~/MyWork
git clone https://github.com/babunalluri/atlas_lite.git ~/MyWork/atlas_lite
cd ~/MyWork/atlas_lite
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### B. Get the migrate pack from OCI

**Chicken-and-egg:** you need SSH into OCI once. Pick one:

1. **OCI Console** → Compute instance → **Cloud Shell / Console connection** (or add this laptop’s `ssh-ed25519.pub` to `opc` authorized_keys for one login), **or**
2. If you already have the OCI key on a USB/AirDrop once, put it at `~/MyWork/OCI/atlas-oci.key.key` and skip to the `scp` below.

Download the pack (from new laptop, after you can SSH as `opc`):

```bash
mkdir -p ~/MyWork/OCI /tmp/atlas-migrate
# If you already have the key:
scp -i ~/MyWork/OCI/atlas-oci.key.key -o IdentitiesOnly=yes \
  opc@137.23.61.107:~/atlas-quant-migrate.tgz /tmp/atlas-quant-migrate.tgz

# Or, from OCI console as opc, then scp/download that file to the new Mac.
```

### C. Unpack into place

```bash
tar -xzf /tmp/atlas-quant-migrate.tgz -C /tmp
PACK=/tmp/atlas-quant-migrate

mkdir -p ~/MyWork/OCI
cp -p "$PACK/keys/atlas-oci.key.key" ~/MyWork/OCI/
cp -p "$PACK/secrets/kite_credentials" ~/MyWork/atlas_lite/
cp -p "$PACK/secrets/llm_credentials" ~/MyWork/atlas_lite/ 2>/dev/null || true
chmod 600 ~/MyWork/OCI/atlas-oci.key.key \
         ~/MyWork/atlas_lite/kite_credentials
chmod 600 ~/MyWork/atlas_lite/llm_credentials 2>/dev/null || true

# Local recordings/ledgers (optional but included)
mkdir -p ~/MyWork/atlas_lite/data
rsync -a "$PACK/repo-local/data/" ~/MyWork/atlas_lite/data/

# Cursor project history (open the repo in Cursor first once, then):
mkdir -p ~/.cursor/projects
# Prefer exact slug if path is ~/MyWork/atlas_lite on this Mac too:
rsync -a "$PACK/cursor-project/" \
  ~/.cursor/projects/Users-$(whoami)-MyWork-atlas-lite/
# If Cursor created a different folder name after first open:
#   ls ~/.cursor/projects | grep -i atlas
#   rsync -a "$PACK/cursor-project/agent-transcripts/" ~/.cursor/projects/<THAT_SLUG>/agent-transcripts/
```

### D. Wipe the pack from OCI (do immediately)

```bash
ssh -i ~/MyWork/OCI/atlas-oci.key.key -o IdentitiesOnly=yes \
  opc@137.23.61.107 'rm -f ~/atlas-quant-migrate.tgz && ls ~/atlas-quant-migrate.tgz 2>&1 || echo pack_removed'
rm -rf /tmp/atlas-quant-migrate /tmp/atlas-quant-migrate.tgz
```

### E. Verify

```bash
cd ~/MyWork/atlas_lite
test -f kite_credentials && test -f ~/MyWork/OCI/atlas-oci.key.key && echo secrets_ok

ssh -i ~/MyWork/OCI/atlas-oci.key.key -o IdentitiesOnly=yes \
  opc@137.23.61.107 \
  'cd ~/atlas_lite && docker compose ps && curl -fsS http://127.0.0.1:3000/health'

# Trading morning: refresh token (syncs to OCI)
python3 scripts/kite_get_access_token.py
```

Open `~/MyWork/atlas_lite` in Cursor. Confirm Live in UI before trading.

---

## What the pack includes

| Path in pack | Goes to on new Mac |
|--------------|--------------------|
| `secrets/kite_credentials` | `~/MyWork/atlas_lite/kite_credentials` |
| `secrets/llm_credentials` | `~/MyWork/atlas_lite/llm_credentials` |
| `keys/atlas-oci.key.key` | `~/MyWork/OCI/atlas-oci.key.key` |
| `cursor-project/` | `~/.cursor/projects/...atlas-lite/` |
| `repo-local/data/` | `~/MyWork/atlas_lite/data/` |
| `docs/` | already also in Git under `doc/` |

Live trading state on the **VM** (`opc@137.23.61.107:~/atlas_lite`) is unchanged — you do not move the server.

---

## On the old computer (after new machine verified)

Quant-only wipe (not whole laptop):

```bash
rm -rf ~/MyWork/atlas_lite
rm -rf ~/MyWork/OCI
rm -rf ~/.cursor/projects/Users-babunalluri-MyWork-atlas-lite
rm -f /tmp/atlas-quant-migrate.tgz
```

---

## Never put in Git

- `kite_credentials`, `llm_credentials`, `.env`
- `atlas-oci.key.key`
- The migrate `.tgz` pack

Code + this doc: normal `git clone` / `git pull` only.
