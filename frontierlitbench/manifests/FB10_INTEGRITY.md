# FB-1.0 historical manifest integrity

The `corpus_sha256_*.json` files record older FB-1.0 snapshots. They are not checksums for every file in the current working tree, and they do not certify the newer FB-1.1 corpus. The current source export has its own `SOURCE_MANIFEST.json`.

An audit of Git history found:

- Track A (video): its single recorded corpus file matches the current bytes.
- Track B (world models): the original 21 recorded files can be reconstructed from Git commit `a2a81ce0`, including historical CRLF bytes.
- Track C (agents): 18 data and documentation files can be reconstructed from commit `e81c2be0c15d2f40a13547c0244ddeec132fa6b3`. The manifest also contains a hash of itself which does not match its committed bytes and cannot be verified.

Later corpus, labels and documentation changes are retained. Do not overwrite the current data or rewrite the old hashes to make the historical record appear current. To reproduce and verify the historical bytes from a Git checkout that contains those commits:

```bash
python scripts/reconstruct_fb10.py --out /path/to/new-directory
```

The script writes separate track directories, the original manifests and a per-file `report.json`. It checks original Git blob bytes and CRLF equivalents against each recorded SHA-256. The agents self-reference is explicitly excluded. The output is a historical snapshot; it does not validate labels or model quality. Source archives without Git history cannot perform this reconstruction on their own.
