# src/nickel_refcats/export_rsp.py
from __future__ import annotations
from pathlib import Path

def _butler_export_copy(butler, outdir: Path, refs, repo_uri: str | None = None, transfer: str = "copy"):
    """
    Call Butler.export with transfer='copy', handling RemoteButler vs local Butler API variants.
    Always pass keyword args (RemoteButler often disallows positional args).

    We try, in order:
      1) repo_uri + (outdir|directory) + (refs|datasets|datasetRefs)
      2) (outdir|directory) + (refs|datasets|datasetRefs)

    Raises the last TypeError if all attempts fail.
    """
    out = str(outdir)
    attempts = []

    # helper to try a single call
    def _try(kwargs):
        attempts.append(kwargs)
        return butler.export(**kwargs)

    errors = []

    # try with repo_uri if provided
    if repo_uri is not None:
        for outkey in ("outdir", "directory"):
            for refkey in ("refs", "datasets", "datasetRefs"):
                try:
                    return _try({ "repo_uri": repo_uri, outkey: out, refkey: refs, "transfer": transfer })
                except TypeError as e:
                    errors.append((outkey, refkey, "with_repo_uri", str(e)))

    # try without repo_uri
    for outkey in ("outdir", "directory"):
        for refkey in ("refs", "datasets", "datasetRefs"):
            try:
                return _try({ outkey: out, refkey: refs, "transfer": transfer })
            except TypeError as e:
                errors.append((outkey, refkey, "no_repo_uri", str(e)))

    # If we get here, none of the signatures matched
    msg = ["Butler.export() did not accept any of the tried keyword signatures."]
    for outkey, refkey, mode, err in errors[:6]:  # show a few examples only
        msg.append(f"  - {mode}: {outkey} + {refkey} → {err}")
    raise TypeError("\n".join(msg))
